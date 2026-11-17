"""fw coverage: how much of the firmware's code the host tests run (gcov).

    fw coverage [PROJECT] [--dir test/host] [--src PATH ...] [--gcda DIR]
                [--lines] [--json] [--min PCT]

It builds the host tests with gcc's --coverage, runs them, and merges what
every test program ran into one line, branch and function count per source
file, so a file two tests share counts once. Which build it uses:

  test_*.c      (fw host-test's default) built here with --coverage
  Makefile      `make coverage` in the test dir: the Makefile's own coverage
                build, which must leave its .gcda files under that dir
  --gcda DIR    no build: read the .gcda files a build already left in DIR

--src limits the report to sources under those paths (from the project root;
default: everything under the project except the test dir). --lines lists
the lines no test ran, --json prints the merged numbers, and --min fails
(exit 1) when line coverage of the reported files is under PCT.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fwlib  # noqa: E402
import hosttest  # noqa: E402


def gcov_json(gcda: Path) -> list[dict]:
    """gcov's JSON for one .gcda: a list of {"file", "lines", "functions"}."""
    r = subprocess.run(["gcov", "--json-format", "--stdout", "--branch-probabilities", gcda.name],
                       cwd=gcda.parent, capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            out += json.loads(line).get("files", [])
    return out


def merge(records: list[dict], root: Path) -> dict[str, dict]:
    """One entry per source file: line -> count, (line, i) -> branch taken, function -> calls."""
    files: dict[str, dict] = {}
    for f in records:
        p = Path(f["file"])
        if not p.is_absolute():
            p = root / p
        key = str(p.resolve())
        e = files.setdefault(key, {"lines": {}, "branches": {}, "functions": {}})
        for ln in f.get("lines", []):
            n = ln["line_number"]
            e["lines"][n] = e["lines"].get(n, 0) + ln["count"]
            for i, b in enumerate(ln.get("branches", [])):
                e["branches"][(n, i)] = e["branches"].get((n, i), 0) + b["count"]
        for fn in f.get("functions", []):
            name = fn["name"]
            prev = e["functions"].get(name, (fn["start_line"], 0))
            e["functions"][name] = (fn["start_line"], prev[1] + fn["execution_count"])
    return files


def summarize(files: dict[str, dict], root: Path, keep) -> list[dict]:
    rows = []
    for path, e in sorted(files.items()):
        if not keep(Path(path)):
            continue
        lines = e["lines"]
        br = e["branches"]
        fns = e["functions"]
        try:
            rel = str(Path(path).relative_to(root))
        except ValueError:
            rel = path
        rows.append({
            "file": rel,
            "lines": len(lines), "lines_run": sum(1 for c in lines.values() if c),
            "branches": len(br), "branches_taken": sum(1 for c in br.values() if c),
            "functions": len(fns), "functions_run": sum(1 for _, c in fns.values() if c),
            "never_called": sorted(n for n, (_, c) in fns.items() if not c),
            "unrun_lines": sorted(n for n, c in lines.items() if not c),
        })
    return rows


def pct(a: int, b: int) -> float:
    return 100.0 * a / b if b else 100.0


def ranges(nums: list[int]) -> str:
    out, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = n
    if start is not None:
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ",".join(out)


def totals(rows: list[dict]) -> dict:
    t = {k: sum(r[k] for r in rows) for k in
         ("lines", "lines_run", "branches", "branches_taken", "functions", "functions_run")}
    t["line_pct"] = round(pct(t["lines_run"], t["lines"]), 1)
    t["branch_pct"] = round(pct(t["branches_taken"], t["branches"]), 1)
    t["function_pct"] = round(pct(t["functions_run"], t["functions"]), 1)
    return t


def report(rows: list[dict], show_lines: bool) -> str:
    w = max([len(r["file"]) for r in rows] + [5])
    out = [f"{'file':<{w}}  lines        branches     functions"]
    for r in sorted(rows, key=lambda r: pct(r["lines_run"], r["lines"])):
        out.append(f"{r['file']:<{w}}  {pct(r['lines_run'], r['lines']):5.1f}% {r['lines']:5d}  "
                   f"{pct(r['branches_taken'], r['branches']):5.1f}% {r['branches']:5d}  "
                   f"{r['functions_run']:3d}/{r['functions']:<3d}")
        if r["never_called"]:
            out.append(f"{'':<{w}}  never called: {', '.join(r['never_called'])}")
        if show_lines and r["unrun_lines"]:
            out.append(f"{'':<{w}}  lines not run: {ranges(r['unrun_lines'])}")
    t = totals(rows)
    out.append(f"{'total':<{w}}  {t['line_pct']:5.1f}% {t['lines']:5d}  "
               f"{t['branch_pct']:5.1f}% {t['branches']:5d}  {t['functions_run']:3d}/{t['functions']:<3d}")
    return "\n".join(out)


def build_simple(project: Path, tdir: Path, out: Path, cc: str) -> list[str]:
    """Build and run each test_*.c with --coverage; return the names that failed."""
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    failed = []
    for t in sorted(tdir.glob("test_*.c")):
        exe = out / t.stem
        cmd = hosttest.compile_cmd(cc, project, tdir, t, exe)
        cmd = [c for c in cmd if not c.startswith("-fsanitize")] + ["--coverage"]
        b = subprocess.run(cmd, cwd=out, capture_output=True, text=True)
        if b.returncode or subprocess.run([str(exe)], cwd=out, capture_output=True, timeout=300).returncode:
            failed.append(t.name)
    return failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fw coverage", description=__doc__.split("\n")[0])
    ap.add_argument("project", nargs="?", type=Path, default=Path.cwd())
    ap.add_argument("--dir")
    ap.add_argument("--src", action="append", default=[])
    ap.add_argument("--gcda", type=Path)
    ap.add_argument("--lines", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min", type=float)
    ap.add_argument("--cc", default=os.environ.get("CC", "gcc"))
    a = ap.parse_args(argv)
    if not shutil.which("gcov"):
        print("fw coverage: no gcov (it comes with gcc)", file=sys.stderr)
        return 2
    project = a.project.resolve()
    pins = fwlib.read_pins(project)
    tdir = project / (a.dir or pins.get("host_tests") or "test/host")
    failed: list[str] = []
    if a.gcda:
        gdir = a.gcda.resolve()
    elif (tdir / "Makefile").is_file():
        if not re.search(r"^coverage\s*:", (tdir / "Makefile").read_text(errors="replace"), re.M):
            print(f"fw coverage: {tdir}/Makefile has no coverage target (build with --coverage, run, "
                  "leave the .gcda files under the test dir), or pass --gcda DIR", file=sys.stderr)
            return 2
        r = subprocess.run(["make", "-C", str(tdir), "coverage", f"FW_UNITY={hosttest.UNITY}"],
                           capture_output=True, text=True)
        if r.returncode:
            print(r.stdout + r.stderr, file=sys.stderr)
            failed.append("make coverage")
        gdir = tdir
    elif tdir.is_dir():
        gdir = project / "build-cov"
        failed = build_simple(project, tdir, gdir, a.cc)
    else:
        print(f"fw coverage: no {tdir} (set host_tests in fw.json)", file=sys.stderr)
        return 2
    gcdas = sorted(gdir.rglob("*.gcda"))
    if not gcdas:
        print(f"fw coverage: no .gcda files under {gdir}", file=sys.stderr)
        return 2
    records: list[dict] = []
    for g in gcdas:
        records += gcov_json(g)
    files = merge(records, project)
    srcs = [(project / s).resolve() for s in a.src]
    tres = tdir.resolve()

    def keep(p: Path) -> bool:
        if srcs:
            return any(p == s or s in p.parents for s in srcs)
        return project in p.parents and tres not in p.parents and hosttest.UNITY not in p.parents

    rows = summarize(files, project, keep)
    t = totals(rows)
    if a.json:
        print(json.dumps({"total": t, "files": rows, "failed": failed}, indent=1))
    else:
        print(report(rows, a.lines))
        if failed:
            print(f"fw coverage: failed to build or run: {', '.join(failed)}")
    if failed:
        return 1
    if a.min is not None and t["line_pct"] < a.min:
        print(f"fw coverage: {t['line_pct']}% of lines run, under --min {a.min}%", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
