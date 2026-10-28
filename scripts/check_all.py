"""fw check-all: build, host-test and QEMU-boot every example, and record it.

    fw check-all [NAME ...] [--keep]

For each examples/<name>/ (or just the ones named) it copies the example to a
scratch dir, runs `fw build`, `fw host-test` and `fw qemu` there, and writes
proof/<name>.json: each stage's verdict, the IDF it ran with, and a hash of
the example's inputs. `fw score` counts the stages that passed on inputs that
still match; the module's test fails when a proof is missing or stale, so an
edited example needs a fresh check-all before it merges. Needs the example's
IDF (fw setup); slow (a first build of an IDF app takes minutes).
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


def stage(name: str, cmd: list[str], log: Path) -> str:
    print(f"fw check-all: {name}: {' '.join(cmd[1:])}", flush=True)
    with open(log, "w") as f:
        r = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
    tail = log.read_text(errors="replace").strip().splitlines()[-1:] or [""]
    print(f"  {'pass' if r.returncode == 0 else 'FAIL'} ({tail[0][:160]})", flush=True)
    return "pass" if r.returncode == 0 else "fail"


def check(name: str, keep: bool) -> dict:
    src = ROOT / "examples" / name
    pins = fwlib.read_pins(src)
    work = Path(tempfile.mkdtemp(prefix=f"fw-check-{name}-"))
    proj = work / name
    shutil.copytree(src, proj, ignore=shutil.ignore_patterns("build", "sdkconfig", "sdkconfig.old"))
    logs = work / "logs"
    logs.mkdir()
    st = {}
    st["build"] = stage("build", [str(FW), "build", str(proj)], logs / "build.log")
    st["host-test"] = stage("host-test", [str(FW), "host-test", str(proj)], logs / "host-test.log")
    st["qemu"] = (stage("qemu", [str(FW), "qemu", str(proj)], logs / "qemu.log")
                  if st["build"] == "pass" else "fail")
    rec = {
        "example": name,
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
    ap.add_argument("--keep", action="store_true", help="keep the scratch dirs and logs")
    a = ap.parse_args(argv)
    names = a.names or score.examples()
    (ROOT / "proof").mkdir(exist_ok=True)
    bad = 0
    for n in names:
        rec = check(n, a.keep)
        (ROOT / "proof" / f"{n}.json").write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
        bad += sum(v != "pass" for v in rec["stages"].values())
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
