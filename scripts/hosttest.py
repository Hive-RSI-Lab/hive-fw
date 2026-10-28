"""fw host-test: run a firmware project's unit tests on this machine, no board.

    fw host-test [PROJECT] [--dir test/host] [-k NAME] [--cc gcc]

The tests live in fw.json's "host_tests" dir (default test/host). The first
of these that fits runs:

  run           an executable script there: it is the test (exit 0 passes)
  CMakeLists.txt  cmake + ctest, with -DFW_UNITY=<this module's Unity> so the
                CMake can add_library(unity ${FW_UNITY}/unity.c)
  Makefile      make test FW_UNITY=...
  test_*.c      the default, no build system: each file is one test program,
                built with cc against Unity and the sources its header names:
                    // fw-src: main/power.c components/codec/es8388_regs.c
                    // fw-inc: main components/codec/include
                    // fw-cflags: -DHOST_TEST
                (paths from the project root; the test dir and Unity are always
                on the include path). Each is run; Unity's exit code is the verdict.

Logic that touches registers or GPIOs is tested by putting the decisions in
plain C (a pure function from inputs to register values or pin states) and
stubbing the IDF calls with a small header in the test dir.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fwlib  # noqa: E402

UNITY = Path(__file__).resolve().parent.parent / "host" / "unity"
_DIRECTIVE = re.compile(r"^\s*//\s*fw-(src|inc|cflags|ldflags):\s*(.*)$", re.M)


def directives(test: Path) -> dict[str, list[str]]:
    """The fw-src/fw-inc/fw-cflags/fw-ldflags lines at the top of a test file."""
    out: dict[str, list[str]] = {"src": [], "inc": [], "cflags": [], "ldflags": []}
    for k, v in _DIRECTIVE.findall(test.read_text(encoding="utf-8", errors="replace")):
        out[k] += shlex.split(v)
    return out


def compile_cmd(cc: str, project: Path, tdir: Path, test: Path, out: Path) -> list[str]:
    d = directives(test)
    cmd = [cc, "-std=gnu11", "-g", "-O0", "-Wall", "-DUNITY_INCLUDE_DOUBLE", "-DFW_HOST_TEST=1",
           "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
           "-I", str(tdir), "-I", str(UNITY)]
    for i in d["inc"]:
        cmd += ["-I", str(project / i)]
    cmd += d["cflags"]
    cmd += [str(test), str(UNITY / "unity.c")] + [str(project / s) for s in d["src"]]
    cmd += ["-o", str(out), "-lm"] + d["ldflags"]
    return cmd


def run_simple(project: Path, tdir: Path, out_dir: Path, cc: str, only: str | None) -> int:
    tests = sorted(tdir.glob("test_*.c"))
    if only:
        tests = [t for t in tests if only in t.stem]
    if not tests:
        print(f"fw host-test: no test_*.c in {tdir}", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    failed = []
    for t in tests:
        exe = out_dir / t.stem
        b = subprocess.run(compile_cmd(cc, project, tdir, t, exe), capture_output=True, text=True)
        if b.returncode:
            print(f"--- {t.name}: BUILD FAILED\n{b.stderr}", flush=True)
            failed.append(t.name)
            continue
        r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=120)
        print(f"--- {t.name}\n{r.stdout}{r.stderr}", end="", flush=True)
        if r.returncode:
            failed.append(t.name)
    print(f"fw host-test: {len(tests) - len(failed)}/{len(tests)} test programs passed"
          + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


def find_cmake() -> list[str] | None:
    if shutil.which("cmake"):
        return ["cmake"]
    for r in fwlib.roots():
        hits = sorted((r / "espressif" / "tools" / "cmake").glob("*/bin/cmake"))
        if hits:
            return [str(hits[-1])]
    uv = shutil.which("uv") or "/hive/tools/uv/bin/uv"
    if os.path.exists(uv):
        return [uv, "tool", "run", "--from", "cmake", "cmake"]
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fw host-test", description=__doc__.split("\n")[0])
    ap.add_argument("project", nargs="?", type=Path, default=Path.cwd())
    ap.add_argument("--dir")
    ap.add_argument("-k", dest="only")
    ap.add_argument("--cc", default=os.environ.get("CC", "cc"))
    a = ap.parse_args(argv)
    project = a.project.resolve()
    pins = fwlib.read_pins(project)
    tdir = project / (a.dir or pins.get("host_tests") or "test/host")
    if not tdir.is_dir():
        print(f"fw host-test: no {tdir} (set host_tests in fw.json)", file=sys.stderr)
        return 2
    out = project / "build" / "host-test"
    env = dict(os.environ, FW_UNITY=str(UNITY), FW_PROJECT=str(project))
    if os.access(tdir / "run", os.X_OK):
        return subprocess.run([str(tdir / "run")], cwd=tdir, env=env).returncode
    if (tdir / "CMakeLists.txt").is_file():
        cmake = find_cmake()
        if not cmake:
            print("fw host-test: no cmake (fw setup installs one; or install uv)", file=sys.stderr)
            return 2
        rc = subprocess.run(cmake + ["-S", str(tdir), "-B", str(out), f"-DFW_UNITY={UNITY}",
                                     f"-DFW_PROJECT={project}"], env=env).returncode
        rc = rc or subprocess.run(cmake + ["--build", str(out), "-j"], env=env).returncode
        ctest = cmake[:-1] + [str(Path(cmake[-1]).with_name("ctest")) if "/" in cmake[-1] else "ctest"]
        return rc or subprocess.run(ctest + ["--test-dir", str(out), "--output-on-failure"], env=env).returncode
    if (tdir / "Makefile").is_file():
        return subprocess.run(["make", "-C", str(tdir), "test", f"FW_UNITY={UNITY}"], env=env).returncode
    return run_simple(project, tdir, out, a.cc, a.only)


if __name__ == "__main__":
    sys.exit(main())
