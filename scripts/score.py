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
passed on the firmware's current inputs (a hash of its files). A target's
files are those on its project's main branch (its "ref", in the clone under
$HIVE_PROJECTS), never the clone's working tree, which may sit on a branch
not merged: a target whose main files this machine can't read has nothing
proven, and a proof made on any other files is stale. The score is
100 x stages proven / stages, higher is better. It reads files only (main's
through git, into a temporary dir); check-all makes the proofs and the test
holds the examples' proofs to their files. A stage that can't pass yet stays counted, unproven: the
score shows what's missing. Prints one line per firmware, then the number.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
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


def project_dir(t: dict) -> Path:
    return Path(os.environ.get("HIVE_PROJECTS", "/hive/projects")) / t["project"]


def target_dir(t: dict) -> Path:
    return project_dir(t) / t.get("path", ".")


def main_files(t: dict, dest: Path) -> Path | str:
    """The target's files as its project's main branch has them (t["ref"],
    default main), written under dest; or why they can't be read. The board
    files its pinmap names come along, at the same paths from it."""
    proj, ref, sub = project_dir(t), t.get("ref", "main"), t.get("path", ".")
    b = t.get("pins", {}).get("pinmap", {}).get("board") or []
    boards = [os.path.normpath(os.path.join(sub, x)) for x in ([b] if isinstance(b, str) else b)]
    boards = [x for x in boards if not x.startswith("..") and not os.path.isabs(x)]
    where = f"{t['project']}@{ref}:{sub}"
    if not proj.is_dir():
        return f"not on this machine ({proj})"
    r = subprocess.run(["git", "-C", str(proj), "archive", "--format=tar", ref, "--", sub, *boards],
                       capture_output=True)
    if r.returncode != 0:
        err = r.stderr.decode(errors="replace").strip().splitlines()[-1:] or [""]
        return f"can't read {where} ({err[0][:120]})"
    try:
        with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
            tf.extractall(dest, filter="data")
    except (tarfile.TarError, OSError) as e:
        return f"can't read {where} ({e})"
    d = dest / sub
    return d if d.is_dir() else f"{where} is not a directory"


def firmware(work: Path, root: Path = ROOT) -> dict[str, Path | str]:
    """Every scored firmware's name and its files, a target's from its
    project's main written under work (a str: why they can't be read)."""
    out: dict[str, Path | str] = {n: root / "examples" / n for n in examples(root)}
    for n, t in targets(root).items():
        (work / n).mkdir(parents=True, exist_ok=True)
        out[n] = main_files(t, work / n)
    return out


def pinmap(root: Path, name: str, src: Path | str | None) -> dict:
    """Where fw pins looks: the target's pins' "pinmap", under the
    firmware's own fw.json's ({} when it names no board)."""
    pm = dict(targets(root).get(name, {}).get("pins", {}).get("pinmap") or {})
    f = (src if isinstance(src, Path) else root / "examples" / name) / "fw.json"
    if f.is_file():
        pm.update(json.loads(f.read_text(encoding="utf-8")).get("pinmap") or {})
    return pm if pm.get("board") else {}


def stages(root: Path, name: str, src: Path | str | None) -> tuple[str, ...]:
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


def proven(root: Path, name: str, src: Path | str) -> tuple[list[str], str]:
    """The stages proof/<name>.json shows passing on current inputs, and why
    none count when it's missing or stale. src a str: inputs can't be read,
    so nothing is proven."""
    if isinstance(src, str):
        return [], f"unproven: {src}"
    p = root / "proof" / f"{name}.json"
    if not p.is_file():
        return [], "no proof (fw check-all)"
    rec = json.loads(p.read_text(encoding="utf-8"))
    if rec.get("inputs") != inputs_hash(src):
        return [], "proof is stale: the files changed since (fw check-all)"
    ok = [s for s in stages(root, name, src) if rec.get("stages", {}).get(s) == "pass"]
    note = ""
    bh = board_hash(src, pinmap(root, name, src)) if PINS in ok else None
    if PINS in ok and (bh is None or rec.get("board") != bh):
        ok.remove(PINS)
        note = "pins proof is stale: the board changed since (fw check-all --only pins)"
    return ok, note


def score(root: Path = ROOT, out=sys.stdout) -> float:
    total = done = 0
    with tempfile.TemporaryDirectory(prefix="fw-score-") as work:
        for n, src in firmware(Path(work), root).items():
            ok, why = proven(root, n, src)
            want = stages(root, n, src)
            total += len(want)
            done += len(ok)
            print(f"{n}: {len(ok)}/{len(want)} {' '.join(ok)}{'  ' + why if why else ''}", file=out)
    return round(100.0 * done / total, 1) if total else 0.0


if __name__ == "__main__":
    print(score())
