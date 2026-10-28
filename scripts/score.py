"""fw score: the share of example stages proven, as a percentage.

    fw score            (or: python3 scripts/score.py)

Every examples/<name>/ has three stages: build, host-test and qemu (a boot
in QEMU that reaches its expect line and stays up). A stage counts when
proof/<name>.json, written by `fw check-all`, records it passed on the
example's current inputs (the hash of its files). The score is
100 x stages proven / stages, higher is better. It reads files only, so it
runs anywhere; check-all is what makes the proofs and the test holds them
to the examples. An example that can't pass a stage yet (a chip QEMU lacks)
keeps the stage counted, unproven: the score shows what's missing.
Prints one line per example, then the number alone.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGES = ("build", "host-test", "qemu")
SKIP = {"build", "build-host", ".git", "__pycache__"}
SKIP_FILES = {"sdkconfig", "sdkconfig.old", "dependencies.lock"}


def examples(root: Path = ROOT) -> list[str]:
    return sorted(p.parent.name for p in (root / "examples").glob("*/fw.json"))


def inputs_hash(example: Path) -> str:
    """sha256 over the example's files (paths and contents), build output left out."""
    h = hashlib.sha256()
    for f in sorted(example.rglob("*")):
        rel = f.relative_to(example)
        if f.is_dir() or rel.parts[0] in SKIP or f.name in SKIP_FILES:
            continue
        h.update(str(rel).encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def proven(root: Path, name: str) -> tuple[list[str], str]:
    """The stages proof/<name>.json shows passing on current inputs, and why
    none count when it's missing or stale."""
    p = root / "proof" / f"{name}.json"
    if not p.is_file():
        return [], "no proof (fw check-all)"
    rec = json.loads(p.read_text(encoding="utf-8"))
    if rec.get("inputs") != inputs_hash(root / "examples" / name):
        return [], "proof is stale: the example changed since (fw check-all)"
    return [s for s in STAGES if rec.get("stages", {}).get(s) == "pass"], ""


def score(root: Path = ROOT, out=sys.stdout) -> float:
    names = examples(root)
    total = done = 0
    for n in names:
        ok, why = proven(root, n)
        total += len(STAGES)
        done += len(ok)
        print(f"{n}: {len(ok)}/{len(STAGES)} {' '.join(ok)}{'  ' + why if why else ''}", file=out)
    return round(100.0 * done / total, 1) if total else 0.0


if __name__ == "__main__":
    print(score())
