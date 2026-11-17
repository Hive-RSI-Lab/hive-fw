"""fw new: start a firmware part from a template, with its host test.

    fw new component NAME [PROJECT] [--from TEMPLATE] [--into DIR] [--force]
    fw new list
    fw new check [TEMPLATE ...] [--keep]

A template is a component that has worked on a board, cut loose from the
board it came from. `fw new component NAME` copies one into the project:

  DIR/                       the component (default PROJECT/components/NAME)
  <host tests>/test_NAME.c   its host test, wired to DIR by fw-src lines
                             (fw.json "host_tests", default test/host)

then `fw host-test PROJECT -k NAME` runs it. NAME is a C identifier stem
(letters, digits, _ or -; a - becomes _ in the C names). TEMPLATE defaults
to NAME when a template of that name exists, else "plain". Nothing is
overwritten without --force.

Each template is templates/component/<T>/ with a component/ tree, a
host_test/ tree and a TEMPLATE.md (its first line is the one-line summary).
In file names and contents @NAME@ becomes the C name, @UNAME@ the same in
capitals, @COMPONENT@ the component's directory name and @DIR@ its path
from the project root. A template.json beside it may list the IDF versions
it is known to build on ({"idf": ["v4.4.8", "v5.5.6"]}).

`fw new check` proves the templates still build: each one goes into a copy
of examples/hello once per IDF it lists (hello's own IDF when it lists
none), then through `fw build` and its host test. The host test alone
misses the IDF side: a callback whose shape changed between IDF versions,
a header that moved. Needs those IDFs (fw setup); slow.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fwlib  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates" / "component"
HELLO = ROOT / "examples" / "hello"
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")


def templates() -> dict[str, str]:
    """template name -> its one-line summary"""
    out = {}
    for d in sorted(TEMPLATES.iterdir()) if TEMPLATES.is_dir() else []:
        if (d / "component").is_dir():
            md = d / "TEMPLATE.md"
            first = md.read_text(encoding="utf-8").strip().splitlines()[0] if md.is_file() else ""
            out[d.name] = first.lstrip("# ").strip()
    return out


def fill(text: str, subs: dict[str, str]) -> str:
    for k, v in subs.items():
        text = text.replace(k, v)
    return text


def plan(template: str, name: str, project: Path, into: Path, tests: Path) -> list[tuple[Path, Path]]:
    """(source in the template, destination) for every file, destinations filled in"""
    cname = name.replace("-", "_")
    subs = {"@NAME@": cname, "@UNAME@": cname.upper(), "@COMPONENT@": into.name}
    root = TEMPLATES / template
    out = []
    for part, dest in (("component", into), ("host_test", tests)):
        base = root / part
        if not base.is_dir():
            continue
        for f in sorted(p for p in base.rglob("*") if p.is_file()):
            out.append((f, dest / fill(str(f.relative_to(base)), subs)))
    return out


def create(template: str, name: str, project: Path, into: Path | None = None, force: bool = False) -> list[Path]:
    """Copy the template in; the files written. Raises ValueError on a bad name, template or clash."""
    if not _NAME.fullmatch(name) or name.endswith(("-", "_")):
        raise ValueError(f"name {name!r}: lower-case letters, digits, _ or -, starting with a letter")
    have = templates()
    if template not in have:
        raise ValueError(f"no template {template!r} (fw new list: {', '.join(have) or 'none'})")
    project = project.resolve()
    into = (into if into is not None else project / "components" / name)
    into = (into if into.is_absolute() else Path.cwd() / into).resolve()
    try:
        rel = into.relative_to(project)
    except ValueError:
        raise ValueError(f"{into} is outside the project {project}: its host test couldn't name it") from None
    pins = fwlib.read_pins(project)
    tests = project / (pins.get("host_tests") or "test/host")
    cname = name.replace("-", "_")
    subs = {"@NAME@": cname, "@UNAME@": cname.upper(), "@COMPONENT@": into.name, "@DIR@": rel.as_posix()}
    files = plan(template, name, project, into, tests)
    clash = [d for _, d in files if d.exists()]
    if clash and not force:
        raise ValueError("already there (--force to overwrite): " + ", ".join(str(c.relative_to(project)) for c in clash))
    for src, dest in files:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(fill(src.read_text(encoding="utf-8"), subs), encoding="utf-8")
    return [d for _, d in files]


def idfs(template: str) -> list[str]:
    """the IDF versions a template says it builds on; hello's when it says none"""
    f = TEMPLATES / template / "template.json"
    listed = json.loads(f.read_text(encoding="utf-8")).get("idf", []) if f.is_file() else []
    return listed or [fwlib.read_pins(HELLO).get("idf", "")]


def check_project(template: str, idf: str, dest: Path) -> Path:
    """examples/hello copied to dest with the template in as `<template>` and fw.json pinned to idf"""
    shutil.copytree(HELLO, dest, ignore=shutil.ignore_patterns("build", "build-*", "sdkconfig", "sdkconfig.old"))
    pins = json.loads((dest / "fw.json").read_text(encoding="utf-8"))
    pins["idf"] = idf
    (dest / "fw.json").write_text(json.dumps(pins, indent=1) + "\n", encoding="utf-8")
    create(template, template.replace("_", "-"), dest)
    return dest


def check(names: list[str], keep: bool = False) -> int:
    have = templates()
    bad = [n for n in names if n not in have]
    if bad:
        print(f"fw new check: no template {', '.join(bad)} (fw new list)", file=sys.stderr)
        return 2
    fw = str(ROOT / "bin" / "fw")
    work = Path(tempfile.mkdtemp(prefix="fw-new-check-"))
    failed = 0
    for t in names or list(have):
        for idf in idfs(t):
            d = check_project(t, idf, work / f"{t}-{idf}")
            for stage in (["build", str(d)], ["host-test", str(d), "-k", "test_" + t.replace("-", "_")]):
                r = subprocess.run([fw, *stage], capture_output=True, text=True)
                ok = r.returncode == 0
                print(f"{t:16} {idf:8} {stage[0]:10} {'ok' if ok else 'FAILED'}")
                if not ok:
                    failed += 1
                    errs = [ln for ln in (r.stdout + r.stderr).splitlines() if "error" in ln.lower()]
                    print("    " + "\n    ".join((errs or (r.stdout + r.stderr).splitlines())[-12:]))
                    break
    if keep or failed:
        print(f"work dirs: {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fw new", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=["component", "list", "check"])
    ap.add_argument("name", nargs="?")
    ap.add_argument("project", nargs="*", default=[])
    ap.add_argument("--from", dest="template")
    ap.add_argument("--into", type=Path, help="the component's directory (default PROJECT/components/NAME)")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--keep", action="store_true", help="check: keep the work dirs")
    a = ap.parse_args(argv)
    if a.kind == "check":
        return check(([a.name] if a.name else []) + a.project, a.keep)
    if len(a.project) > 1:
        ap.error("one PROJECT")
    a.project = Path(a.project[0]) if a.project else Path.cwd()
    have = templates()
    if a.kind == "list":
        for t, summary in have.items():
            print(f"{t:16} {summary}")
        return 0
    if not a.name:
        ap.error("fw new component NAME")
    template = a.template or (a.name if a.name in have else "plain")
    try:
        files = create(template, a.name, a.project, a.into, a.force)
    except ValueError as e:
        print(f"fw new: {e}", file=sys.stderr)
        return 2
    project = a.project.resolve()
    print(f"fw new: {a.name} from template {template}:")
    for f in files:
        print("  " + str(f.relative_to(project)))
    md = TEMPLATES / template / "TEMPLATE.md"
    if md.is_file():
        print(f"  (read {md} for what it does and what it was measured at)")
    print(f"next: fw host-test {project} -k test_{a.name.replace('-', '_')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
