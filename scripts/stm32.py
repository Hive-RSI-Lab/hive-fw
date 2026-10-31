"""fw stm32: STM32 firmware with arm-none-eabi-gcc, no IDF.

    fw stm32 setup [--prefix DIR]        install the pinned xPack arm-none-eabi-gcc
    fw stm32 doctor                      which toolchain is found, and where
    fw stm32 build [PROJECT] [-- ARGS]   make, with ARM_PREFIX set to the toolchain
    fw stm32 pins [PROJECT] [--json]     the firmware's pins against the chip and the board

Host unit tests are `fw host-test` as for any project. Flashing is dfu-util
on the chip's ROM DFU (0483:df11; a blank chip needs BOOT0 high, AN2606).

The toolchain is xPack arm-none-eabi-gcc 14.2.1-1.1 (sha256 pinned), under
the same roots as the IDF installs (/hive/tools/fw, else $FW_HOME). A project
may pin another with fw.json "stm32": {"toolchain": "14.2.1-1.1"} only if
this script knows its sha256 (TOOLCHAINS); ARM_PREFIX in the environment wins.

`build` runs `make` in PROJECT (default .) with ARM_PREFIX=<bin>/arm-none-eabi-
(the variable the Makefile should honour); extra make args follow `--`.

`pins` reads fw.json "stm32":
    {"chip": "stm32g474_lqfp64",             (data/NAME.json, or a path)
     "header": "src/hw/board_pins.h",        (#define PIN_X PB, 3 and
                                              #define ADC_X 1, 5 lines)
     "pinmap": "../../boards/x/pinmap.json", (optional; signals -> {"pin","function"})
     "signals": ["SPI_SCK", ...],            (must be assigned; optional)
     "fixed": {"USB_DM": "PA11"}}            (signals the chip pins down)
Checks: every pin exists on the package, none is used twice, an ADC input
is on a pin that carries that channel, fixed signals sit where the chip puts
them, listed signals are assigned, and firmware and board agree pin for pin.
Exit 0 all pass, 1 a fault, 2 couldn't run. Chip data is parsed from the
datasheet's pin tables (data/ has the G474 LQFP64, DS12288 Rev 6).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fwlib  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"
DEFAULT_TOOLCHAIN = "14.2.1-1.1"
TOOLCHAINS = {  # version -> sha256 of the linux-x64 tarball
    "14.2.1-1.1": "ed8c7d207a85d00da22b90cf80ab3b0b2c7600509afadf6b7149644e9d4790a6",
}
URL = ("https://github.com/xpack-dev-tools/arm-none-eabi-gcc-xpack/releases/download/"
       "v{v}/xpack-arm-none-eabi-gcc-{v}-linux-x64.tar.gz")


def log(msg: str) -> None:
    print(f"fw stm32: {msg}", file=sys.stderr, flush=True)


def project_pins(project: Path) -> dict:
    f = project / "fw.json"
    return json.loads(f.read_text(encoding="utf-8")).get("stm32", {}) if f.is_file() else {}


def toolchain_version(project: Path) -> str:
    v = project_pins(project).get("toolchain", DEFAULT_TOOLCHAIN)
    if v not in TOOLCHAINS:
        raise ValueError(f"toolchain {v}: no pinned sha256 (known: {', '.join(TOOLCHAINS)})")
    return v


def toolchain_dir(root: Path, v: str) -> Path:
    return root / f"xpack-arm-none-eabi-gcc-{v}"


def find_prefix(v: str) -> str | None:
    """ARM_PREFIX (env), else the pinned xPack in the first root, else a gcc on PATH."""
    if os.environ.get("ARM_PREFIX"):
        return os.environ["ARM_PREFIX"]
    for r in fwlib.roots():
        d = toolchain_dir(r, v)
        if (d / ".fw-ok").is_file():
            return str(d / "bin" / "arm-none-eabi-")
    gcc = shutil.which("arm-none-eabi-gcc")
    return gcc[:-3] if gcc else None


def install(prefix: Path, v: str) -> Path:
    dest = toolchain_dir(prefix, v)
    prefix.mkdir(parents=True, exist_ok=True)
    with open(prefix / ".fw-lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        if (dest / ".fw-ok").is_file():
            return dest
        tar = prefix / f"xpack-arm-none-eabi-gcc-{v}.tar.gz"
        log(f"downloading {URL.format(v=v)}")
        with urllib.request.urlopen(URL.format(v=v), timeout=300) as r, open(tar, "wb") as f:
            shutil.copyfileobj(r, f)
        h = hashlib.sha256()
        with open(tar, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != TOOLCHAINS[v]:
            tar.unlink()
            raise RuntimeError(f"sha256 {h.hexdigest()}, want {TOOLCHAINS[v]}")
        shutil.rmtree(dest, ignore_errors=True)
        with tarfile.open(tar) as t:
            t.extractall(prefix, filter="data")
        tar.unlink()
        (dest / ".fw-ok").write_text(v + "\n")
    return dest


# ---- pins ---------------------------------------------------------------

def load_chip(name: str, project: Path) -> dict:
    p = Path(name)
    if not p.suffix:
        p = DATA / f"{name}.json"
    elif not p.is_absolute():
        p = project / p
    return json.loads(p.read_text(encoding="utf-8"))


def parse_header(path: Path) -> tuple[dict, dict]:
    pins, adcs = {}, {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"#define\s+PIN_(\w+)\s+([A-G])\s*,\s*(\d+)", line)
        if m:
            pins[m.group(1)] = f"P{m.group(2)}{int(m.group(3))}"
        m = re.match(r"#define\s+ADC_(\w+)\s+(\d)\s*,\s*(\d+)", line)
        if m:
            adcs[m.group(1)] = (int(m.group(2)), int(m.group(3)))
    return pins, adcs


def chip_has_adc(chip: dict, pin: str, adc: int, ch: int) -> bool:
    for name in chip["pins"].get(pin, {}).get("adc", []):
        m = re.match(r"ADC(\d+)_IN(\d+)$", name)
        if m and int(m.group(2)) == ch and str(adc) in m.group(1):
            return True
    return False


def board_pin(entry) -> str:
    return entry["pin"] if isinstance(entry, dict) else entry


def check_pins(chip: dict, pins: dict, adcs: dict, signals: list, fixed: dict,
               board: dict | None) -> tuple[list[str], list[str]]:
    errs, notes = [], []
    for s in signals:
        if s not in pins:
            errs.append(f"{s}: not assigned in the header")
    for s, p in fixed.items():
        if pins.get(s) and pins[s] != p:
            errs.append(f"{s}: {pins[s]}, the chip puts it on {p}")
    seen: dict = {}
    for s, p in pins.items():
        if p not in chip["pins"]:
            errs.append(f"{s}: {p} is not a pin on {chip.get('package', 'the package')}")
        if p in seen:
            errs.append(f"{p}: used by both {seen[p]} and {s}")
        seen[p] = s
    for s, (adc, ch) in adcs.items():
        p = pins.get(s)
        if not p:
            errs.append(f"{s}: ADC channel given but no pin")
        elif not chip_has_adc(chip, p, adc, ch):
            errs.append(f"{s}: {p} does not carry ADC{adc}_IN{ch} (it has {chip['pins'].get(p, {}).get('adc')})")
    if board is not None:
        sig = dict(board.get("signals", {}))
        sig.update(board.get("other", {}))
        for s in signals:
            if s not in sig:
                errs.append(f"{s}: missing from the board's pinmap")
        for s, e in sig.items():
            if s in pins and board_pin(e) != pins[s]:
                errs.append(f"{s}: firmware {pins[s]}, board {board_pin(e)}")
            elif s not in pins and s not in signals:
                notes.append(f"{s} ({board_pin(e)}) is on the board, not used by firmware")
            if isinstance(e, dict) and s in adcs:
                adc, ch = adcs[s]
                if not re.search(rf"ADC\d*{adc}\d*_IN{ch}\b", e.get("function", "")):
                    errs.append(f"{s}: board says '{e.get('function', '')}', firmware reads ADC{adc}_IN{ch}")
    return errs, notes


def cmd_pins(a) -> int:
    project = Path(a.project).resolve()
    cfg = project_pins(project)
    if "chip" not in cfg or "header" not in cfg:
        log('fw.json needs "stm32": {"chip": ..., "header": ...}')
        return 2
    try:
        chip = load_chip(cfg["chip"], project)
        pins, adcs = parse_header(project / cfg["header"])
        board = None
        if cfg.get("pinmap"):
            board = json.loads((project / cfg["pinmap"]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log(str(e))
        return 2
    if not pins:
        log(f"no PIN_ defines in {cfg['header']}")
        return 2
    errs, notes = check_pins(chip, pins, adcs, cfg.get("signals", []), cfg.get("fixed", {}), board)
    if a.json:
        print(json.dumps({"pins": len(pins), "adc": len(adcs), "errors": errs, "notes": notes}, indent=1))
    else:
        for n in notes:
            print("note:", n)
        for e in errs:
            print("FAIL:", e)
        if not errs:
            print(f"pins OK: {len(pins)} pins, {len(adcs)} ADC inputs, against "
                  f"{'the board and ' if board else ''}{chip.get('source', 'the chip')}")
    return 1 if errs else 0


# ---- the rest -----------------------------------------------------------

def cmd_setup(a) -> int:
    v = toolchain_version(Path(a.project).resolve())
    if not a.prefix and find_prefix(v):
        log(f"toolchain ready: {find_prefix(v)}gcc")
        return 0
    log(f"toolchain ready: {install(Path(a.prefix or fwlib.home_root()).resolve(), v)}")
    return 0


def cmd_doctor(a) -> int:
    v = toolchain_version(Path(a.project).resolve())
    p = find_prefix(v)
    print(f"toolchain pin: {v}")
    print(f"ARM_PREFIX: {p or 'not found (fw stm32 setup)'}")
    if p:
        r = subprocess.run([p + "gcc", "--version"], capture_output=True, text=True)
        print(r.stdout.splitlines()[0] if r.returncode == 0 and r.stdout else "gcc does not run")
    print(f"dfu-util: {shutil.which('dfu-util') or 'not found'}")
    return 0 if p else 1


def cmd_build(a) -> int:
    project = Path(a.project).resolve()
    p = find_prefix(toolchain_version(project))
    if not p:
        log("no arm-none-eabi toolchain: run fw stm32 setup")
        return 2
    extra = [x for x in a.make if x != "--"]
    return subprocess.call(["make", f"ARM_PREFIX={p}"] + extra, cwd=project)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fw stm32", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("setup", cmd_setup), ("doctor", cmd_doctor), ("build", cmd_build), ("pins", cmd_pins)):
        s = sub.add_parser(name)
        s.add_argument("project", nargs="?", default=".")
        s.set_defaults(fn=fn)
        if name == "setup":
            s.add_argument("--prefix")
        if name == "pins":
            s.add_argument("--json", action="store_true")
        if name == "build":
            s.add_argument("make", nargs=argparse.REMAINDER)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
