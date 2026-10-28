"""fw score: the share of firmware stages proven, as a percentage.

    fw score            (or: python3 scripts/score.py)

What's scored is every firmware fw serves, each with three stages: build,
host-test and qemu (a boot in QEMU that reaches its expect line and stays
up):
  - examples/<name>/: this module's own examples;
  - targets.json: real products in other Hive projects, first the hifi
    receiver's Lite firmware (hifi-receiver-2's firmware/).
A stage counts when proof/<name>.json, written by `fw check-all`, records it
passed on the firmware's current inputs (a hash of its files). A target whose
files this machine can't see ($HIVE_PROJECTS) is judged on its proof alone.
The score is 100 x stages proven / stages, higher is better. It reads files
only; check-all makes the proofs and the test holds the examples' proofs
to their files. A stage that can't pass yet stays counted, unproven: the
score shows what's missing. Prints one line per firmware, then the number.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGES = ("build", "host-test", "qemu")
SKIP = {"build", "build-host", "build-qemu", ".fw-src", ".git", "__pycache__"}
SKIP_FILES = {"sdkconfig", "sdkconfig.old", "dependencies.lock"}


def examples(root: Path = ROOT) -> list[str]:
    return sorted(p.parent.name for p in (root / "examples").glob("*/fw.json"))


def targets(root: Path = ROOT) -> dict[str, dict]:
    """targets.json: {name: {"project": p, "path": dir in it, "pins": {...}}}."""
    f = root / "targets.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


def target_dir(t: dict) -> Path:
    return Path(os.environ.get("HIVE_PROJECTS", "/hive/projects")) / t["project"] / t.get("path", ".")


def firmware(root: Path = ROOT) -> dict[str, Path | None]:
    """Every scored firmware's name and its files (None: not on this machine)."""
    out: dict[str, Path | None] = {n: root / "examples" / n for n in examples(root)}
    for n, t in targets(root).items():
        d = target_dir(t)
        out[n] = d if d.is_dir() else None
    return out


def inputs_hash(example: Path) -> str:
    """sha256 over the firmware's files (paths and contents), build output left out."""
    h = hashlib.sha256()
    for f in sorted(example.rglob("*")):
        rel = f.relative_to(example)
        if f.is_dir() or rel.parts[0] in SKIP or f.name in SKIP_FILES:
            continue
        h.update(str(rel).encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def proven(root: Path, name: str, src: Path | None = None) -> tuple[list[str], str]:
    """The stages proof/<name>.json shows passing on current inputs, and why
    none count when it's missing or stale. src None: inputs can't be seen."""
    p = root / "proof" / f"{name}.json"
    if not p.is_file():
        return [], "no proof (fw check-all)"
    rec = json.loads(p.read_text(encoding="utf-8"))
    note = ""
    if src is None:
        note = "  (files not on this machine: proof taken as is)"
    elif rec.get("inputs") != inputs_hash(src):
        return [], "proof is stale: the files changed since (fw check-all)"
    return [s for s in STAGES if rec.get("stages", {}).get(s) == "pass"], note.strip()


def score(root: Path = ROOT, out=sys.stdout) -> float:
    total = done = 0
    for n, src in firmware(root).items():
        ok, why = proven(root, n, src)
        total += len(STAGES)
        done += len(ok)
        print(f"{n}: {len(ok)}/{len(STAGES)} {' '.join(ok)}{'  ' + why if why else ''}", file=out)
    return round(100.0 * done / total, 1) if total else 0.0


if __name__ == "__main__":
    print(score())
