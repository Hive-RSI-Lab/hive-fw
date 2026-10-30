"""fw score: how many planted firmware faults fw's checks catch.

    fw score            (or: python3 scripts/score.py)

The corpus is faults/<base>/: a copy of a real firmware's files (and its
board's netlist) with a fw.json saying where things are (`fw constraints`,
fw's one checker), and faults.json, the planted faults: each a list of
edits (find, replace; each find must occur exactly once), planted in a
scratch copy of the base. A fault is caught when the checker reports a
problem with it planted that it doesn't report on the clean base. Each
fault names its kind ("expect", the check it's for) and its "site" (the
signal, setting or partition it breaks): a second fault of the same kind at
the same site is a near-duplicate and counts nothing, so only a distinct
real mistake raises the score. The score is faults caught minus false
alarms (problems on a clean base),
higher is better and unbounded: a new kind of fault, a new check, or a new
base raises it. It reads the corpus only, nothing outside this module, so it
reads the same in any sandbox. Prints one line per fault, then the number.

A fault with "blind" (the Espressif document and section that says it's a
mistake) was written by someone who hadn't read the checker, so it shows
what the checker misses. Its misses are printed last as the to-do list: the
checks fw doesn't have yet, each a point when it lands. A miss costs nothing.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import constraints  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
# memory, not disk, for the scratch copies: on a loaded machine disk writes stall
SCRATCH = "/dev/shm" if os.access("/dev/shm", os.W_OK) else None


def bases(root: Path = ROOT) -> list[Path]:
    return sorted(p.parent for p in (root / "faults").glob("*/faults.json"))


def errors(base: Path) -> set[str]:
    """The checker's problems on the firmware under base, as CODE: message."""
    fwpins, cfg = constraints.config(base, None)
    res = constraints.build_report(base, fwpins, cfg, None)
    return {f"{x['code']}: {x['problem']}" for x in res["problems"]}


def plant(base: Path, work: Path, edits: list[dict]) -> str:
    """Plant the edits in work, a copy of base; or why they can't be."""
    files: dict[str, str] = {}
    for e in edits:
        text = files.get(e["file"])
        if text is None:
            text = (base / e["file"]).read_text(encoding="utf-8")
        n = text.count(e["find"])
        if n != 1:
            return f"{e['file']}: {e['find']!r} found {n} times"
        files[e["file"]] = text.replace(e["find"], e["replace"])
    for f, text in files.items():
        (work / f).write_text(text, encoding="utf-8")
    return ""


def run(root: Path = ROOT, out=sys.stdout) -> dict:
    """{"caught": n, "faults": n, "false_alarms": n, "results": [...]}."""
    caught = total = alarms = 0
    results = []
    todo = []
    for b in bases(root):
        work = Path(tempfile.mkdtemp(prefix=f"fw-score-{b.name}-", dir=SCRATCH))
        try:
            shutil.copytree(b, work, dirs_exist_ok=True)
            clean = errors(work)
            alarms += len(clean)
            for x in sorted(clean):
                print(f"{b.name}: false alarm on the clean base: {x}", file=out)
            seen: set[tuple[str, str]] = set()
            for fault in json.loads((b / "faults.json").read_text(encoding="utf-8"))["faults"]:
                key = (fault.get("expect", ""), fault.get("site", fault["id"]))
                if key in seen:
                    results.append({"base": b.name, "id": fault["id"], "caught": False, "by": [],
                                    "broken": "", "duplicate": True})
                    print(f"{b.name}/{fault['id']}: DUPLICATE of another {key[0]} fault at {key[1]}; not counted",
                          file=out)
                    continue
                seen.add(key)
                total += 1
                why = plant(b, work, fault["edits"])
                new = sorted(errors(work) - clean) if not why else []
                for e in fault["edits"]:  # back to the clean base for the next fault
                    shutil.copyfile(b / e["file"], work / e["file"])
                hit = bool(new)
                caught += hit
                by = sorted({x.split(":", 1)[0] for x in new})
                results.append({"base": b.name, "id": fault["id"], "caught": hit, "by": by, "broken": why,
                                "duplicate": False, "blind": bool(fault.get("blind"))})
                if fault.get("blind") and not hit and not why:
                    todo.append(f"  {b.name}/{fault['id']} ({fault.get('expect', '?')} at {fault.get('site', '?')}): "
                                f"{fault.get('about', '')} [{fault['blind']}]")
                line = f"caught by {', '.join(by)}" if hit else f"BROKEN: {why}" if why else \
                    f"missed (meant for {fault.get('expect', '?')})"
                print(f"{b.name}/{fault['id']}: {line}", file=out)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    if not total:
        raise SystemExit("fw score: no faults under faults/*/faults.json")
    blind = [x for x in results if x.get("blind")]
    if todo:
        print(f"to do: {len(todo)} blind fault(s) missed, each a check fw doesn't have yet:", file=out)
        for t in todo:
            print(t, file=out)
    print(f"{caught}/{total} faults caught ({sum(x['caught'] for x in blind)}/{len(blind)} blind), "
          f"{alarms} false alarm(s)", file=out)
    return {"caught": caught, "faults": total, "false_alarms": alarms, "results": results, "todo": todo}


def score(root: Path = ROOT, out=sys.stdout) -> int:
    r = run(root, out)
    return r["caught"] - r["false_alarms"]


if __name__ == "__main__":
    print(score())
