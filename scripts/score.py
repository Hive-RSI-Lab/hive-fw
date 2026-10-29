"""fw score: the share of firmware stages proven, as a percentage.

    fw score            (or: python3 scripts/score.py)

What's scored is every firmware fw serves, each with three stages: build,
host-test and qemu (a boot in QEMU that reaches its expect line and stays
up), and a fourth, pins (fw pins: its pin map against the board's netlist),
for firmware whose pins name a board ("pinmap"):
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
PINS = "pins"
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


def pinmap(root: Path, name: str, src: Path | None) -> dict:
    """Where fw pins looks: the target's pins' "pinmap", under the
    firmware's own fw.json's ({} when it names no board)."""
    pm = dict(targets(root).get(name, {}).get("pins", {}).get("pinmap") or {})
    f = (src or root / "examples" / name) / "fw.json"
    if f.is_file():
        pm.update(json.loads(f.read_text(encoding="utf-8")).get("pinmap") or {})
    return pm if pm.get("board") else {}


def stages(root: Path, name: str, src: Path | None) -> tuple[str, ...]:
    return STAGES + ((PINS,) if pinmap(root, name, src) else ())


def board_files(src: Path, pm: dict) -> list[Path]:
    b = pm.get("board") or []
    return [(src / x).resolve() for x in ([b] if isinstance(b, str) else b)]


def board_hash(src: Path, pm: dict) -> str | None:
    """sha256 over the board files fw pins reads; None when one isn't here."""
    h = hashlib.sha256()
    for f in board_files(src, pm):
        if not f.is_file():
            return None
        h.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


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
    ok = [s for s in stages(root, name, src) if rec.get("stages", {}).get(s) == "pass"]
    if PINS in ok and src is not None:
        bh = board_hash(src, pinmap(root, name, src))
        if bh is not None and rec.get("board") != bh:
            ok.remove(PINS)
            note = "pins proof is stale: the board changed since (fw check-all --only pins)"
    return ok, note.strip()


def score(root: Path = ROOT, out=sys.stdout) -> float:
    total = done = 0
    for n, src in firmware(root).items():
        ok, why = proven(root, n, src)
        want = stages(root, n, src)
        total += len(want)
        done += len(ok)
        print(f"{n}: {len(ok)}/{len(want)} {' '.join(ok)}{'  ' + why if why else ''}", file=out)
    return round(100.0 * done / total, 1) if total else 0.0


if __name__ == "__main__":
    print(score())
