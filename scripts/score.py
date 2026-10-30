"""fw score: how many planted firmware faults fw's checks catch.

    fw score            (or: python3 scripts/score.py)

The corpus is faults/<base>/: a copy of a real firmware's files (and its
board's netlist) with a fw.json saying where things are (`fw constraints`,
fw's one checker), and faults.json, the planted faults: each a list of
edits (find, replace; each find must occur exactly once), planted in a
scratch copy of the base. A fault is caught when the checker reports a
problem with it planted that it doesn't report on the clean base. The score is faults caught minus false alarms (errors on a clean base),
higher is better and unbounded: a new kind of fault, a new check, or a new
base raises it. It reads the corpus only, nothing outside this module, so it
reads the same in any sandbox. Prints one line per fault, then the number.
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
    for b in bases(root):
        work = Path(tempfile.mkdtemp(prefix=f"fw-score-{b.name}-", dir=SCRATCH))
        try:
            shutil.copytree(b, work, dirs_exist_ok=True)
            clean = errors(work)
            alarms += len(clean)
            for x in sorted(clean):
                print(f"{b.name}: false alarm on the clean base: {x}", file=out)
            for fault in json.loads((b / "faults.json").read_text(encoding="utf-8"))["faults"]:
                total += 1
                why = plant(b, work, fault["edits"])
                new = sorted(errors(work) - clean) if not why else []
                for e in fault["edits"]:  # back to the clean base for the next fault
                    shutil.copyfile(b / e["file"], work / e["file"])
                hit = bool(new)
                caught += hit
                by = sorted({x.split(":", 1)[0] for x in new})
                results.append({"base": b.name, "id": fault["id"], "caught": hit, "by": by, "broken": why})
                line = f"caught by {', '.join(by)}" if hit else f"BROKEN: {why}" if why else \
                    f"missed (meant for {fault.get('expect', '?')})"
                print(f"{b.name}/{fault['id']}: {line}", file=out)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    if not total:
        raise SystemExit("fw score: no faults under faults/*/faults.json")
    print(f"{caught}/{total} faults caught, {alarms} false alarm(s)", file=out)
    return {"caught": caught, "faults": total, "false_alarms": alarms, "results": results}


def score(root: Path = ROOT, out=sys.stdout) -> int:
    r = run(root, out)
    return r["caught"] - r["false_alarms"]


if __name__ == "__main__":
    print(score())
