"""fw check-all: build, host-test and QEMU-boot every scored firmware, and record it.

    fw check-all [NAME ...] [--keep] [--from DIR]

The firmware are examples/<name>/ and the targets in targets.json (real
products in other Hive projects, read from $HIVE_PROJECTS; --from DIR reads
a single named target from DIR instead, say a branch not yet merged). Each is
copied to a work dir and run through `fw build`, `fw host-test` and `fw qemu`
(after `fw build --qemu` when its pins give QEMU its own sdkconfig). Then
proof/<name>.json records each stage's verdict, the IDF it ran with, and a
hash of the firmware's files, which `fw score` counts while they still
match. A target's pins in targets.json fill in what its own fw.json lacks,
and its IDF tree is kept under $FW_HOME/check/<name>/src between runs, so a
re-check rebuilds incrementally. Needs the firmware's IDF (fw setup); slow.
"""
from __future__ import annotations

import argparse
import datetime
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fwlib  # noqa: E402
import score  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "bin" / "fw"
IGNORE = shutil.ignore_patterns(*score.SKIP, *score.SKIP_FILES)


def stage(name: str, cmd: list[str], log: Path) -> str:
    print(f"fw check-all: {name}: {' '.join(cmd[1:])}", flush=True)
    with open(log, "w") as f:
        r = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
    tail = log.read_text(errors="replace").strip().splitlines()[-1:] or [""]
    print(f"  {'pass' if r.returncode == 0 else 'FAIL'} ({tail[0][:160]})", flush=True)
    return "pass" if r.returncode == 0 else "fail"


def stage_into(src: Path, work: Path, pins_over: dict | None, name: str) -> Path:
    """The firmware's files copied to work/<name>, with the target's pins
    merged under its own fw.json and its IDF tree kept between runs."""
    proj = work / name
    if proj.exists():
        shutil.rmtree(proj)
    shutil.copytree(src, proj, ignore=IGNORE)
    if pins_over is not None:
        f = proj / "fw.json"
        own = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
        pins = {**pins_over, **own}
        if pins.get("prepare"):
            pins["src"] = str(fwlib.home_root() / "check" / name / "src")
        f.write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8")
    return proj


def check(name: str, src: Path, pins_over: dict | None, keep: bool) -> dict:
    work = Path(tempfile.mkdtemp(prefix=f"fw-check-{name}-"))
    proj = stage_into(src, work, pins_over, name)
    pins = fwlib.read_pins(proj)
    logs = work / "logs"
    logs.mkdir()
    st = {}
    st["build"] = stage("build", [str(FW), "build", str(proj)], logs / "build.log")
    st["host-test"] = stage("host-test", [str(FW), "host-test", str(proj)], logs / "host-test.log")
    ok = st["build"] == "pass"
    if ok and pins.get("qemu", {}).get("sdkconfig"):
        ok = stage("build --qemu", [str(FW), "build", str(proj), "--qemu", "--no-prepare"],
                   logs / "build-qemu.log") == "pass"
    st["qemu"] = stage("qemu", [str(FW), "qemu", str(proj)], logs / "qemu.log") if ok else "fail"
    rec = {
        "firmware": name,
        "idf": pins["idf"],
        "target": pins["target"],
        "inputs": score.inputs_hash(src),
        "stages": st,
        "when": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    if keep:
        print(f"  kept {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    return rec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fw check-all", description=__doc__.split("\n")[0])
    ap.add_argument("names", nargs="*")
    ap.add_argument("--keep", action="store_true", help="keep the work dirs and logs")
    ap.add_argument("--from", dest="src", type=Path, help="the one named target's files, from here")
    a = ap.parse_args(argv)
    tg = score.targets()
    found = score.firmware()
    names = a.names or list(found)
    if a.src and (len(names) != 1 or names[0] not in tg):
        ap.error("--from takes exactly one target name from targets.json")
    (ROOT / "proof").mkdir(exist_ok=True)
    bad = 0
    for n in names:
        src = a.src.resolve() if a.src else found.get(n)
        if src is None:
            print(f"fw check-all: {n}: not on this machine ({score.target_dir(tg[n])}); skipped")
            continue
        rec = check(n, src, tg[n].get("pins", {}) if n in tg else None, a.keep)
        (ROOT / "proof" / f"{n}.json").write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
        bad += sum(v != "pass" for v in rec["stages"].values())
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
