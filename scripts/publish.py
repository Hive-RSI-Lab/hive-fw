"""fw publish: hand a project's build to a board bench, where a person flashes it from their browser.

    fw publish [PROJECT] [--name NAME] [--build DIR] [--no-post] [-- BENCH ARGS...]

The bench is `serialtab` (a Hive's "plug in a board" page), found on PATH or at
$FW_SERIALTAB. fw doesn't carry it: without it, fw publish says so and exits 3,
and the build stays where `fw build` left it (fw flash still flashes it over a port).

It runs `serialtab publish BUILD NAME --chip CHIP [--profile bench.json] --post`:
  BUILD    the IDF build dir (PROJECT/build, or the prepared tree's), which has flash_args
  NAME     fw.json "name", else the project folder's name, lower-cased to letters, digits and -
  CHIP     from fw.json "target": esp32 -> ESP32, esp32s3 -> ESP32-S3 (the bench checks the chip it finds)
  profile  fw.json "bench" (a path from the project), else PROJECT/bench.json when it is there
  --post   the bench posts a "flash this build" link in the agent's thread; --no-post leaves it out
BENCH ARGS go to serialtab publish as they are (--label, --version, --thread T).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fwlib  # noqa: E402

NO_BENCH = 3


def die(msg: str, code: int = 2) -> int:
    print(f"fw publish: {msg}", file=sys.stderr)
    return code


def bench_name(pins: dict, project: Path) -> str:
    """fw.json "name", else the folder's: lower-case letters, digits and -, at most 41 (the bench's rule)."""
    raw = pins.get("name") or project.name
    n = re.sub(r"[^a-z0-9]+", "-", str(raw).lower()).strip("-")[:41].strip("-")
    if not n:
        raise ValueError(f"no bench name from {raw!r}: set \"name\" in fw.json")
    return n


def chip_of(target: str) -> str:
    """IDF's target as the chip name esptool reports: esp32 -> ESP32, esp32s3 -> ESP32-S3, esp32c3 -> ESP32-C3."""
    m = re.fullmatch(r"esp32([a-z]\d+)?", target or "esp32")
    if not m:
        raise ValueError(f"target {target!r}: fw publish knows the ESP32 family (esp32, esp32s3, esp32c3, ...)")
    return "ESP32" + ("-" + m.group(1).upper() if m.group(1) else "")


def find_bench() -> str | None:
    return os.environ.get("FW_SERIALTAB") or shutil.which("serialtab")


def plan(project: Path, args: list[str]) -> tuple[list[str], Path]:
    """The bench command (with the bench program left out) and the build dir it publishes."""
    name = build = None
    post, rest = True, []
    it = iter(args)
    for a in it:
        if a == "--":
            rest += list(it)
        elif a == "--name":
            name = next(it, None)
        elif a == "--build":
            build = next(it, None)
        elif a == "--no-post":
            post = False
        else:
            raise ValueError(f"unknown argument {a!r} (bench arguments go after --)")
    pins = fwlib.read_pins(project)
    bdir = Path(build).resolve() if build else fwlib.src_dir(project, pins) / "build"
    if not (bdir / "flash_args").is_file():
        raise FileNotFoundError(f"no build in {bdir} (no flash_args): run fw build {project} first")
    prof = Path(project, pins["bench"]) if pins.get("bench") else project / "bench.json"
    if pins.get("bench") and not prof.is_file():
        raise FileNotFoundError(f"fw.json bench: {prof} isn't there")
    cmd = ["publish", str(bdir), bench_name(pins, project) if not name else name, "--chip", chip_of(pins["target"])]
    if prof.is_file():
        cmd += ["--profile", str(prof)]
    if post:
        cmd.append("--post")
    return cmd + rest, bdir


def main(argv: list[str]) -> int:
    if argv[:1] in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    project = Path.cwd()
    if argv and not argv[0].startswith("-") and Path(argv[0]).is_dir():
        project, argv = Path(argv[0]).resolve(), argv[1:]
    try:
        cmd, bdir = plan(project, argv)
    except (ValueError, FileNotFoundError) as e:
        return die(str(e))
    bench = find_bench()
    if not bench:
        return die(f"no board bench here: serialtab isn't installed (not on PATH, no $FW_SERIALTAB). "
                   f"The build is in {bdir}; flash it over a port with fw flash {project} --port P.", NO_BENCH)
    print("fw: serialtab " + " ".join(cmd), file=sys.stderr, flush=True)
    return subprocess.run([bench] + cmd).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
