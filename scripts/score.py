"""fw score: how many planted firmware faults fw's checks catch.

    fw score            (or: python3 scripts/score.py)

The corpus is faults/<base>/: a copy of a real firmware's files (and its
board's netlist) with a lint.json saying where things are (`fw lint`), and
faults.json, the planted faults: each a list of edits (find, replace; each
find must occur exactly once), read in place of the base's files. A fault is
caught when fw's checks report an error with it planted that they don't
report on the clean base. Each fault names its kind ("expect", the check it's for) and its "site"
(the signal, setting or partition it breaks): a second fault of the same
kind at the same site is a near-duplicate and counts nothing, so only a
distinct real mistake raises the score. The score is faults caught minus
false alarms (errors on a clean base),
higher is better and unbounded: a new kind of fault, a new check, or a new
base raises it. It reads the corpus only, nothing outside this module, so it
reads the same in any sandbox. Prints one line per fault, then the number.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lint  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def bases(root: Path = ROOT) -> list[Path]:
    return sorted(p.parent for p in (root / "faults").glob("*/faults.json"))


def errors(base: Path, files: dict[str, str] | None = None) -> set[str]:
    spec = json.loads((base / "lint.json").read_text(encoding="utf-8"))
    return {str(f) for f in lint.lint(base, spec, files) if f.error}


def plant(base: Path, edits: list[dict]) -> tuple[dict[str, str], str]:
    """The files the edits change, as they read with the fault planted; or
    why it can't be."""
    files: dict[str, str] = {}
    for e in edits:
        text = files.get(e["file"])
        if text is None:
            text = (base / e["file"]).read_text(encoding="utf-8")
        n = text.count(e["find"])
        if n != 1:
            return {}, f"{e['file']}: {e['find']!r} found {n} times"
        files[e["file"]] = text.replace(e["find"], e["replace"])
    return files, ""


def run(root: Path = ROOT, out=sys.stdout) -> dict:
    """{"caught": n, "faults": n, "false_alarms": n, "results": [...]}."""
    caught = total = alarms = 0
    results = []
    for b in bases(root):
        clean = errors(b)
        alarms += len(clean)
        for x in sorted(clean):
            print(f"{b.name}: false alarm on the clean base: {x}", file=out)
        seen: set[tuple[str, str]] = set()
        for fault in json.loads((b / "faults.json").read_text(encoding="utf-8"))["faults"]:
            key = (fault.get("expect", ""), fault.get("site", fault["id"]))
            if key in seen:
                results.append({"base": b.name, "id": fault["id"], "caught": False, "by": [],
                                "broken": "", "duplicate": True})
                print(f"{b.name}/{fault['id']}: DUPLICATE of another {key[0]} fault at {key[1]}; not counted", file=out)
                continue
            seen.add(key)
            total += 1
            files, why = plant(b, fault["edits"])
            new = sorted(errors(b, files) - clean) if not why else []
            hit = bool(new)
            caught += hit
            by = sorted({x.split(":", 1)[0] for x in new})
            results.append({"base": b.name, "id": fault["id"], "caught": hit, "by": by, "broken": why,
                            "duplicate": False})
            line = f"caught by {', '.join(by)}" if hit else f"BROKEN: {why}" if why else \
                f"missed (meant for {fault.get('expect', '?')})"
            print(f"{b.name}/{fault['id']}: {line}", file=out)
    if not total:
        raise SystemExit("fw score: no faults under faults/*/faults.json")
    print(f"{caught}/{total} faults caught, {alarms} false alarm(s)", file=out)
    return {"caught": caught, "faults": total, "false_alarms": alarms, "results": results}


def score(root: Path = ROOT, out=sys.stdout) -> int:
    r = run(root, out)
    return r["caught"] - r["false_alarms"]


if __name__ == "__main__":
    print(score())
