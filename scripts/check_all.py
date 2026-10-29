"""fw check-all: build, host-test and QEMU-boot every scored firmware, and record it.

    fw check-all [NAME ...] [--keep] [--from DIR] [--only pins]

The firmware are examples/<name>/ and the targets in targets.json (real
products in other Hive projects, read from $HIVE_PROJECTS; --from DIR reads
a single named target from DIR instead, say a branch not yet merged). Each is
copied to a work dir and run through `fw build`, `fw host-test` and `fw qemu`
(after `fw build --qemu` when its pins give QEMU its own sdkconfig). Then
proof/<name>.json records each stage's verdict, the IDF it ran with, and a
hash of the firmware's files, which `fw score` counts while they still
match. Firmware whose pins name a board ("pinmap") gets a fourth stage,
`fw pins` on its own files (not the copy, so the board's path from them
holds), and the proof keeps a hash of the board files too. --only pins runs
just that stage, keeping the other stages of a proof made on the same files;
it needs no IDF, so a board change can be re-proven in a second. A target's pins in targets.json fill in what its own fw.json lacks,
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


def pins_stage(name: str, src: Path, is_target: bool, log: Path) -> str:
    return stage("pins", [str(FW), "pins", str(src), *(["--target", name] if is_target else [])], log)


def check_pins(name: str, src: Path, is_target: bool) -> dict:
    """--only pins: the pins stage over the proof's other stages, which must
    have been made on these same files."""
    p = ROOT / "proof" / f"{name}.json"
    rec = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    st = {k: v for k, v in rec.get("stages", {}).items() if k != score.PINS}
    if rec.get("inputs") != score.inputs_hash(src):
        print(f"fw check-all: {name}: its proof isn't on these files; the other stages are left out (run them too)")
        st = {k: "not run" for k in score.STAGES}
    with tempfile.TemporaryDirectory(prefix=f"fw-pins-{name}-") as d:
        st[score.PINS] = pins_stage(name, src, is_target, Path(d) / "pins.log")
    return {**rec, "firmware": name, "inputs": score.inputs_hash(src), "stages": st,
            "board": score.board_hash(src, score.pinmap(ROOT, name, src)),
            "when": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}


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
    pm = score.pinmap(ROOT, name, src)
    if pm:
        st[score.PINS] = pins_stage(name, src, pins_over is not None, logs / "pins.log")
    rec = {
        "firmware": name,
        "idf": pins["idf"],
        "target": pins["target"],
        "inputs": score.inputs_hash(src),
        "stages": st,
        **({"board": score.board_hash(src, pm)} if pm else {}),
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
    ap.add_argument("--only", choices=[score.PINS], help="run just this stage (no IDF needed)")
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
        if a.only:
            if not score.pinmap(ROOT, n, src):
                print(f"fw check-all: {n}: names no board (pinmap); skipped")
                continue
            rec = check_pins(n, src, n in tg)
        else:
            rec = check(n, src, tg[n].get("pins", {}) if n in tg else None, a.keep)
        (ROOT / "proof" / f"{n}.json").write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
        bad += sum(v != "pass" for v in rec["stages"].values())
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
