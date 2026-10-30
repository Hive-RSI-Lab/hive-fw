"""fw check-all: build, host-test and QEMU-boot every firmware fw knows.

    fw check-all [NAME ...] [--keep] [--from DIR]

The firmware are examples/<name>/ and the targets in targets.json (real
products in other Hive projects, read from their main branch in the clone
under $HIVE_PROJECTS; --from DIR reads a single named target from DIR
instead, say a branch not yet merged). Each is copied to a work dir and run
through `fw build`, `fw host-test` and `fw qemu` (after `fw build --qemu`
when its pins give QEMU its own sdkconfig). An example's run is recorded in
proof/<name>.json (each stage's verdict, the IDF, a hash of its files),
which the test holds to its files; a target's is printed only: a product's
own check proves its firmware, and its record lives in its project. A
target's pins in targets.json fill in what its own fw.json lacks, and its
IDF tree is kept under $FW_HOME/check/<name>/src between runs, so a re-check
rebuilds incrementally. Needs the firmware's IDF (fw setup); slow.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fwlib  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "bin" / "fw"
STAGES = ("build", "host-test", "qemu")
SKIP = {"build", "build-host", "build-qemu", ".fw-src", ".git", "__pycache__"}
SKIP_FILES = {"sdkconfig", "sdkconfig.old", "dependencies.lock"}


def examples(root: Path = ROOT) -> list[str]:
    return sorted(p.parent.name for p in (root / "examples").glob("*/fw.json"))


def targets(root: Path = ROOT) -> dict[str, dict]:
    """targets.json: {name: {"project": p, "path": dir in it, "pins": {...}}}."""
    f = root / "targets.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


def project_dir(t: dict) -> Path:
    return Path(os.environ.get("HIVE_PROJECTS", "/hive/projects")) / t["project"]


def target_dir(t: dict) -> Path:
    return project_dir(t) / t.get("path", ".")


def main_files(t: dict, dest: Path) -> Path | str:
    """The target's files as its project's main branch has them (t["ref"],
    default main), written under dest; or why they can't be read."""
    proj, ref, sub = project_dir(t), t.get("ref", "main"), t.get("path", ".")
    where = f"{t['project']}@{ref}:{sub}"
    if not proj.is_dir():
        return f"not on this machine ({proj})"
    r = subprocess.run(["git", "-C", str(proj), "archive", "--format=tar", ref, "--", sub],
                       capture_output=True)
    if r.returncode != 0:
        err = r.stderr.decode(errors="replace").strip().splitlines()[-1:] or [""]
        return f"can't read {where} ({err[0][:120]})"
    try:
        with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
            tf.extractall(dest, filter="data")
    except (tarfile.TarError, OSError) as e:
        return f"can't read {where} ({e})"
    d = dest / sub
    return d if d.is_dir() else f"{where} is not a directory"


def firmware(work: Path, root: Path = ROOT) -> dict[str, Path | str]:
    """Every checked firmware's name and its files, a target's from its
    project's main written under work (a str: why they can't be read)."""
    out: dict[str, Path | str] = {n: root / "examples" / n for n in examples(root)}
    for n, t in targets(root).items():
        (work / n).mkdir(parents=True, exist_ok=True)
        out[n] = main_files(t, work / n)
    return out


def inputs_hash(example: Path) -> str:
    """sha256 over the firmware's files (paths and contents), build output left out."""
    h = hashlib.sha256()
    for f in sorted(example.rglob("*")):
        rel = f.relative_to(example)
        if f.is_dir() or rel.parts[0] in SKIP or f.name in SKIP_FILES:
            continue
        h.update(str(rel).encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def proven(root: Path, name: str, src: Path | str) -> tuple[list[str], str]:
    """The stages proof/<name>.json shows passing on an example's current
    inputs, and why none count when it's missing or stale."""
    if isinstance(src, str):
        return [], f"unproven: {src}"
    p = root / "proof" / f"{name}.json"
    if not p.is_file():
        return [], "no proof (fw check-all)"
    rec = json.loads(p.read_text(encoding="utf-8"))
    if rec.get("inputs") != inputs_hash(src):
        return [], "proof is stale: the files changed since (fw check-all)"
    return [s for s in STAGES if rec.get("stages", {}).get(s) == "pass"], ""


IGNORE = shutil.ignore_patterns(*SKIP, *SKIP_FILES)


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
    if ok and fwlib.qemu_variant(pins):
        ok = stage("build --qemu", [str(FW), "build", str(proj), "--qemu", "--no-prepare"],
                   logs / "build-qemu.log") == "pass"
    st["qemu"] = stage("qemu", [str(FW), "qemu", str(proj)], logs / "qemu.log") if ok else "fail"
    rec = {
        "firmware": name,
        "idf": pins["idf"],
        "target": pins["target"],
        "inputs": inputs_hash(src),
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
    tg = targets()
    tmp = tempfile.TemporaryDirectory(prefix="fw-main-")
    found = firmware(Path(tmp.name))
    names = a.names or list(found)
    if a.src and (len(names) != 1 or names[0] not in tg):
        ap.error("--from takes exactly one target name from targets.json")
    (ROOT / "proof").mkdir(exist_ok=True)
    bad = 0
    for n in names:
        src = a.src.resolve() if a.src else found.get(n, f"no firmware {n}")
        if isinstance(src, str):
            print(f"fw check-all: {n}: {src}; skipped")
            bad += 1
            continue
        rec = check(n, src, tg[n].get("pins", {}) if n in tg else None, a.keep)
        if n not in tg:
            (ROOT / "proof" / f"{n}.json").write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
        bad += sum(v != "pass" for v in rec["stages"].values())
    tmp.cleanup()
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
