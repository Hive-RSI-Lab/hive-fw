"""fw new: start a firmware part, or a whole app, from a template, with its host test.

    fw new component NAME [PROJECT] [--from TEMPLATE] [--into DIR] [--force]
    fw new app NAME [DIR] [--from TEMPLATE] [--force]
    fw new list
    fw new check [TEMPLATE ...] [--keep]

A template is a component that has worked on a board, cut loose from the
board it came from. `fw new component NAME` copies one into the project:

  DIR/                       the component (default PROJECT/components/NAME)
  <host tests>/test_NAME.c   its host test, wired to DIR by fw-src lines
                             (fw.json "host_tests", default test/host)
  <fuzz dir>/fuzz_NAME.c     its libFuzzer harness and seeds/fuzz_NAME/, when
                             it has one (fw.json "fuzz_tests", default test/fuzz)

then `fw host-test PROJECT -k NAME` runs it. NAME is a C identifier stem
(letters, digits, _ or -; a - becomes _ in the C names). TEMPLATE defaults
to NAME when a template of that name exists, else "plain". Nothing is
overwritten without --force.

Each template is templates/component/<T>/ with a component/ tree, a
host_test/ tree, an optional fuzz/ tree (a harness for `fw fuzz`, its seeds
under seeds/, copied byte for byte) and a TEMPLATE.md (its first line is the
one-line summary).
In file names and contents @NAME@ becomes the C name, @UNAME@ the same in
capitals, @COMPONENT@ the component's directory name and @DIR@ its path
from the project root. A template.json beside it may list the IDF versions
it is known to build on ({"idf": ["v4.4.8", "v5.5.6"]}) and the sdkconfig
lines it needs ({"sdkconfig": ["CONFIG_BT_ENABLED=y"]}): `fw new component`
adds to PROJECT/sdkconfig.defaults, under a comment naming the component,
each one whose option the file doesn't set already, under its name or an
older one IDF still reads (CONFIG_BLUEDROID_ENABLED for
CONFIG_BT_BLUEDROID_ENABLED). An option from a Kconfig choice is not added
when the file picks another member (NimBLE where Bluedroid is wanted): that
is a warning to settle by hand. A project configured before keeps its
sdkconfig: set the lines in menuconfig too (deleting sdkconfig would drop
every menuconfig change that isn't in the defaults). sdkconfig.defaults.<target>
files are not read.

An app template is a whole project to start a board's firmware from:
templates/app/<T>/ with a project/ tree, a TEMPLATE.md (first line the
summary) and a template.json listing its IDFs. `fw new app NAME [DIR]`
copies project/ into DIR (default ./NAME), with @NAME@ (the C name) and
@UNAME@ filled in names and contents; then `fw build DIR` and
`fw host-test DIR`. TEMPLATE defaults to remote-ready: an app the Hive's
bench can test, update and roll back over Wi-Fi. DIR must be new or empty
unless --force (which writes over the template's own files and leaves the
rest).

`fw new check` proves the templates still build: each component template
goes into a copy of examples/hello once per IDF it lists (hello's own IDF
when it lists none), then through `fw build` and its host test; each app
template is copied out once per IDF it lists, its fw.json pinned to that
IDF, then through `fw build` and `fw host-test`. The host test alone
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
APP_TEMPLATES = ROOT / "templates" / "app"
DEFAULT_APP = "remote-ready"
HELLO = ROOT / "examples" / "hello"
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")


def _summaries(root: Path, part: str) -> dict[str, str]:
    out = {}
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if (d / part).is_dir():
            md = d / "TEMPLATE.md"
            first = md.read_text(encoding="utf-8").strip().splitlines()[0] if md.is_file() else ""
            out[d.name] = first.lstrip("# ").strip()
    return out


def templates() -> dict[str, str]:
    """component template name -> its one-line summary"""
    return _summaries(TEMPLATES, "component")


def app_templates() -> dict[str, str]:
    """app template name -> its one-line summary"""
    return _summaries(APP_TEMPLATES, "project")


def fill(text: str, subs: dict[str, str]) -> str:
    for k, v in subs.items():
        text = text.replace(k, v)
    return text


def plan(template: str, name: str, project: Path, into: Path, tests: Path,
         fuzz: Path | None = None) -> list[tuple[Path, Path]]:
    """(source in the template, destination) for every file, destinations filled in"""
    cname = name.replace("-", "_")
    subs = {"@NAME@": cname, "@UNAME@": cname.upper(), "@COMPONENT@": into.name}
    root = TEMPLATES / template
    out = []
    for part, dest in (("component", into), ("host_test", tests), ("fuzz", fuzz or project / "test/fuzz")):
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
    fuzz = project / (pins.get("fuzz_tests") or "test/fuzz")
    cname = name.replace("-", "_")
    subs = {"@NAME@": cname, "@UNAME@": cname.upper(), "@COMPONENT@": into.name, "@DIR@": rel.as_posix()}
    files = plan(template, name, project, into, tests, fuzz)
    clash = [d for _, d in files if d.exists()]
    if clash and not force:
        raise ValueError("already there (--force to overwrite): " + ", ".join(str(c.relative_to(project)) for c in clash))
    seeds = TEMPLATES / template / "fuzz" / "seeds"
    for src, dest in files:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_relative_to(seeds):  # fuzzer inputs: bytes, not text
            dest.write_bytes(src.read_bytes())
        else:
            dest.write_text(fill(src.read_text(encoding="utf-8"), subs), encoding="utf-8")
    return [d for _, d in files]


def create_app(template: str, name: str, dest: Path, force: bool = False) -> list[Path]:
    """Copy the app template's project/ to dest, names filled in; the files written.
    Raises ValueError on a bad name or template, or a dest with files in it (without force)."""
    if not _NAME.fullmatch(name) or name.endswith(("-", "_")):
        raise ValueError(f"name {name!r}: lower-case letters, digits, _ or -, starting with a letter")
    have = app_templates()
    if template not in have:
        raise ValueError(f"no app template {template!r} (fw new list: {', '.join(have) or 'none'})")
    dest = (dest if dest.is_absolute() else Path.cwd() / dest).resolve()
    if dest.exists() and not dest.is_dir():
        raise ValueError(f"{dest} is a file")
    if dest.is_dir() and any(dest.iterdir()) and not force:
        raise ValueError(f"{dest} has files in it already (--force to write the template's over them)")
    cname = name.replace("-", "_")
    subs = {"@NAME@": cname, "@UNAME@": cname.upper()}
    base = APP_TEMPLATES / template / "project"
    out = []
    for f in sorted(p for p in base.rglob("*") if p.is_file()):
        d = dest / fill(str(f.relative_to(base)), subs)
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_text(fill(f.read_text(encoding="utf-8"), subs), encoding="utf-8")
        out.append(d)
    return out


def template_json(template: str, root: Path = TEMPLATES) -> dict:
    f = root / template / "template.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


def _key(line: str) -> str | None:
    """CONFIG_X for "CONFIG_X=v" and "# CONFIG_X is not set" (fwidf.sdkconfig_key)"""
    line = line.strip()
    if line.startswith("CONFIG_") and "=" in line:
        return line.split("=", 1)[0]
    if line.startswith("# CONFIG_") and line.endswith(" is not set"):
        return line[2:-len(" is not set")]
    return None


# older names IDF still reads (its components' sdkconfig.rename), for the
# options templates set
OLD_NAMES = {
    "CONFIG_BT_BLUEDROID_ENABLED": ["CONFIG_BLUEDROID_ENABLED"],
    "CONFIG_BT_NIMBLE_ENABLED": ["CONFIG_NIMBLE_ENABLED"],
    "CONFIG_BT_GATTS_ENABLE": ["CONFIG_GATTS_ENABLE"],
    "CONFIG_BT_GATTC_ENABLE": ["CONFIG_GATTC_ENABLE"],
}
# Kconfig choices: setting one member =y picks it over the others
CHOICES = [["CONFIG_BT_BLUEDROID_ENABLED", "CONFIG_BT_NIMBLE_ENABLED"]]


def _names(key: str) -> set[str]:
    return {key, *OLD_NAMES.get(key, [])}


def _defaults(project: Path) -> tuple[Path, str, dict[str, str]]:
    """PROJECT/sdkconfig.defaults, its text, and {option: line} for what it sets"""
    f = project / "sdkconfig.defaults"
    text = f.read_text(encoding="utf-8") if f.is_file() else ""
    have = {}
    for ln in text.splitlines():
        k = _key(ln)
        if k:
            have[k] = ln.strip()
    return f, text, have


def _picks_other(key: str, have: dict[str, str]) -> str | None:
    """the member of key's choice that the project picks instead, if any"""
    for group in CHOICES:
        if key in group:
            for other in group:
                if other != key and any(have.get(n, "").endswith("=y") for n in _names(other)):
                    return other
    return None


def sdkconfig_warnings(template: str, project: Path) -> list[str]:
    """the template's choice options the project's sdkconfig.defaults picks otherwise"""
    _, _, have = _defaults(project)
    out = []
    for ln in template_json(template).get("sdkconfig") or []:
        k = _key(ln)
        other = k and ln.strip().endswith("=y") and _picks_other(k, have)
        if other:
            out.append(f"{template} wants {k}, but sdkconfig.defaults picks {other}: not changed, settle it by hand")
    return out


def add_sdkconfig(template: str, name: str, project: Path) -> list[str]:
    """Append the template's sdkconfig lines that PROJECT/sdkconfig.defaults
    doesn't set yet (under any of their names), except a choice member when
    the file picks another (sdkconfig_warnings); the lines added."""
    want = template_json(template).get("sdkconfig") or []
    if not want:
        return []
    f, text, have = _defaults(project)
    add = [ln for ln in want
           if _key(ln) and not (_names(_key(ln)) & have.keys()) and not _picks_other(_key(ln), have)]
    if add:
        sep = "" if not text or text.endswith("\n") else "\n"
        f.write_text(text + sep + f"# {name} (fw new, template {template})\n" + "\n".join(add) + "\n", encoding="utf-8")
    return add


def idfs(template: str) -> list[str]:
    """the IDF versions a template says it builds on; hello's when it says none"""
    listed = template_json(template).get("idf", [])
    return listed or [fwlib.read_pins(HELLO).get("idf", "")]


def check_project(template: str, idf: str, dest: Path) -> Path:
    """examples/hello copied to dest with the template in as `<template>` and fw.json pinned to idf"""
    shutil.copytree(HELLO, dest, ignore=shutil.ignore_patterns("build", "build-*", "sdkconfig", "sdkconfig.old"))
    pins = json.loads((dest / "fw.json").read_text(encoding="utf-8"))
    pins["idf"] = idf
    (dest / "fw.json").write_text(json.dumps(pins, indent=1) + "\n", encoding="utf-8")
    create(template, template.replace("_", "-"), dest)
    add_sdkconfig(template, template.replace("_", "-"), dest)
    return dest


def app_idfs(template: str) -> list[str]:
    """the IDF versions an app template says it builds on; its own fw.json's when it says none"""
    listed = template_json(template, APP_TEMPLATES).get("idf", [])
    return listed or [fwlib.read_pins(APP_TEMPLATES / template / "project").get("idf", "")]


def check_app(template: str, idf: str, dest: Path) -> Path:
    """the app template copied to dest as an app named after it, fw.json pinned to idf"""
    create_app(template, template.replace("_", "-"), dest)
    pins = json.loads((dest / "fw.json").read_text(encoding="utf-8"))
    pins["idf"] = idf
    (dest / "fw.json").write_text(json.dumps(pins, indent=1) + "\n", encoding="utf-8")
    return dest


def check(names: list[str], keep: bool = False) -> int:
    have, apps = templates(), app_templates()
    bad = [n for n in names if n not in have and n not in apps]
    if bad:
        print(f"fw new check: no template {', '.join(bad)} (fw new list)", file=sys.stderr)
        return 2
    fw = str(ROOT / "bin" / "fw")
    work = Path(tempfile.mkdtemp(prefix="fw-new-check-"))
    failed = 0
    runs = [(t, idf, "component") for t in (names or list(have)) if t in have for idf in idfs(t)]
    runs += [(t, idf, "app") for t in (names or list(apps)) if t in apps and t not in have for idf in app_idfs(t)]
    for t, idf, kind in runs:
        if kind == "component":
            d = check_project(t, idf, work / f"{t}-{idf}")
            stages = (["build", str(d)], ["host-test", str(d), "-k", "test_" + t.replace("-", "_")])
        else:
            d = check_app(t, idf, work / f"app-{t}-{idf}")
            stages = (["build", str(d)], ["host-test", str(d)])
        for stage in stages:
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
    ap.add_argument("kind", choices=["component", "app", "list", "check"])
    ap.add_argument("name", nargs="?")
    ap.add_argument("project", nargs="*", default=[])
    ap.add_argument("--from", dest="template")
    ap.add_argument("--into", type=Path, help="the component's directory (default PROJECT/components/NAME)")
    ap.add_argument("--force", action="store_true", help="write over files already there")
    ap.add_argument("--keep", action="store_true", help="check: keep the work dirs")
    a = ap.parse_args(argv)
    if a.kind == "check":
        return check(([a.name] if a.name else []) + a.project, a.keep)
    if len(a.project) > 1:
        ap.error("one PROJECT" if a.kind != "app" else "one DIR")
    if a.kind == "app":
        return new_app(ap, a)
    a.project = Path(a.project[0]) if a.project else Path.cwd()
    have = templates()
    if a.kind == "list":
        for t, summary in have.items():
            print(f"{t:16} {summary}")
        apps = app_templates()
        if apps:
            print("\napps (fw new app NAME [DIR] --from T):")
            for t, summary in apps.items():
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
    added = add_sdkconfig(template, a.name, project)
    if added:
        print(f"  sdkconfig.defaults: added {', '.join(added)}")
        if (project / "sdkconfig").is_file():
            print("  (sdkconfig was made before: set those in menuconfig too, for them to take;"
                  " deleting sdkconfig would drop menuconfig changes not in the defaults)")
    for w in sdkconfig_warnings(template, project):
        print(f"  warning: {w}", file=sys.stderr)
    md = TEMPLATES / template / "TEMPLATE.md"
    if md.is_file():
        print(f"  (read {md} for what it does and what it was measured at)")
    print(f"next: fw host-test {project} -k test_{a.name.replace('-', '_')}")
    return 0


def new_app(ap: argparse.ArgumentParser, a: argparse.Namespace) -> int:
    if not a.name:
        ap.error("fw new app NAME [DIR]")
    if a.into:
        ap.error("--into is for a component; an app goes in DIR")
    template = a.template or DEFAULT_APP
    dest = Path(a.project[0]) if a.project else Path(a.name)
    try:
        files = create_app(template, a.name, dest, a.force)
    except ValueError as e:
        print(f"fw new: {e}", file=sys.stderr)
        return 2
    dest = (dest if dest.is_absolute() else Path.cwd() / dest).resolve()
    print(f"fw new: app {a.name} from template {template}, in {dest} ({len(files)} files)")
    md = APP_TEMPLATES / template / "TEMPLATE.md"
    if md.is_file():
        print(f"  (read {md} for what it does and how to set a board up for it)")
    print(f"next: fw build {dest} && fw host-test {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
