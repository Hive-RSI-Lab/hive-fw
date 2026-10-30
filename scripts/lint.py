"""fw lint: what a firmware gets wrong about its chip and its board, read
from its files without building it.

    fw lint [DIR] [--spec FILE]     exit 0: no errors, 1: errors, 2: bad spec

DIR's lint.json (or --spec) says where the firmware keeps what's read, paths
relative to DIR (each optional):
  "headers": C headers whose `#define NAME <gpio>` (NAME with PIN, GPIO, SDA
      or SCL in it) and `#define NAME ADCn_CHANNEL_k` give the pins it uses;
  "sources": C files whose gpio_set_level(NAME, ...) mark NAME an output;
  "kconfig": {"file": F, "when": SYMBOL}: Kconfig `default "..." if SYMBOL`
      strings (squeezelite-esp32's: bck=27,ws=25,..., "gpio":13 buttons,
      "14=ir:nec,22=green:1") give more pins; F may be a patch adding them;
  "sdkconfig": sdkconfig(.defaults): flash size, PSRAM, Wi-Fi;
  "partitions": the partition table CSV;
  "sizes": {"<partition>": bytes} of the images a build put there;
  "netlist": the board's KiCad netlist, "module": the ESP32's reference in it.

The checks (each finding is CODE: message):
  PIN-NET      a pin's net on the board doesn't carry its name (wrong GPIO),
               or the pad is unconnected
  PIN-SHARED   one GPIO given two unrelated jobs
  PIN-FLASH    GPIO6-11 (the SPI flash) or, with PSRAM, GPIO16/17
  PIN-INPUT    GPIO34-39 (input only) used as an output
  STRAP-12     GPIO12 (MTDI) used: high at reset selects 1.8 V flash
  ADC2-WIFI    an ADC2 channel read while Wi-Fi is on (ADC2 is Wi-Fi's)
  PART-*       partitions overlapping, past the flash, misaligned, over the
               table, an OTA app without otadata, an image bigger than its slot
Notes (not errors): board nets on the module that no firmware pin names.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ADC1 = {0: 36, 1: 37, 2: 38, 3: 39, 4: 32, 5: 33, 6: 34, 7: 35}
ADC2 = {0: 4, 1: 0, 2: 2, 3: 15, 4: 13, 5: 12, 6: 14, 7: 27, 8: 25, 9: 26}
FLASH_PINS = {6, 7, 8, 9, 10, 11}
PSRAM_PINS = {16, 17}
INPUT_ONLY = {34, 35, 36, 37, 38, 39}
PADS = {"SENSOR_VP": 36, "SENSOR_CAPP": 37, "SENSOR_CAPN": 38, "SENSOR_VN": 39,
        "TXD0": 1, "RXD0": 3, "SD2": 9, "SD3": 10, "CMD": 11, "CLK": 6, "SD0": 7, "SD1": 8}
# Kconfig keys that name a GPIO, the name a board gives that signal, and
# whether the firmware drives it.
KEYS = {"bck": ("BCLK", True), "ws": ("LRCK", True), "do": ("DOUT", True),
        "di": ("DIN", False), "mck": ("MCLK", True), "sda": ("SDA", True),
        "scl": ("SCL", True), "mdc": ("MDC", True), "mdio": ("MDIO", True),
        "rst": ("RST", True), "mute": ("MUTE", True), "clk": ("CLK", True)}
# One spelling for words boards and firmware spell differently.
ALIAS = {"BCK": "BCLK", "SCK": "BCLK", "WS": "LRCK", "LRC": "LRCK", "DO": "DOUT",
         "SDO": "DOUT", "DI": "DIN", "SDI": "DIN", "MCK": "MCLK", "VOLDOWN": "VOLDN",
         "TOGGLE": "PLAY", "GREEN": "LED", "RED": "LED", "BLUE": "LED"}
GENERIC = {"PIN", "GPIO", "IO", "ADC", "NUM", "CH", "CHANNEL", "ACTRLS", "BTN", "NET", "DRV"}
FLASH_SIZES = {"1MB": 1 << 20, "2MB": 2 << 20, "4MB": 4 << 20, "8MB": 8 << 20,
               "16MB": 16 << 20, "32MB": 32 << 20}


@dataclass(frozen=True)
class Pin:
    gpio: int
    name: str        # where it came from: a macro, a Kconfig key
    words: frozenset  # what it is, for matching the board's net names
    out: bool = False
    adc: int = 0      # 1 or 2: read as an analog channel of that ADC


@dataclass(frozen=True)
class Finding:
    code: str
    msg: str
    error: bool = True

    def __str__(self) -> str:
        return f"{self.code}: {self.msg}" if self.error else f"note: {self.msg}"


def words(name: str) -> frozenset:
    ws = (ALIAS.get(w, w) for w in re.split(r"[^A-Z0-9]+", name.upper()) if w)
    return frozenset(w for w in ws if w not in GENERIC and not w.isdigit())


def header_pins(text: str, outputs: set[str]) -> list[Pin]:
    pins = []
    for name, val in re.findall(r"^\s*#\s*define\s+(\w+)\s+([^\s/]+)", text, re.M):
        m = re.fullmatch(r"ADC([12])_CHANNEL_(\d+)", val)
        if m:
            unit, ch = int(m.group(1)), int(m.group(2))
            g = (ADC1 if unit == 1 else ADC2).get(ch)
            if g is not None:
                pins.append(Pin(g, name, words(name), False, unit))
            continue
        if re.fullmatch(r"\(?-?\d+\)?", val) and re.search(r"PIN|GPIO|SDA$|SCL$|_IO$", name):
            g = int(val.strip("()"))
            if 0 <= g <= 39:
                pins.append(Pin(g, name, words(name), name in outputs))
    return pins


def kconfig_strings(text: str, when: str) -> list[str]:
    out = []
    for line in text.splitlines():
        if line.startswith("-"):
            continue
        m = re.match(r'^\+?\s*default\s+"((?:[^"\\]|\\.)*)"\s+if\s+(.+)$', line)
        if m and re.search(rf"\b{re.escape(when)}\b", m.group(2)):
            out.append(m.group(1).replace('\\"', '"'))
    return out


def kconfig_pins(text: str, when: str) -> list[Pin]:
    pins = []
    for s in kconfig_strings(text, when):
        if s.lstrip().startswith("["):
            try:
                for b in json.loads(s):
                    act = (b.get("normal") or {}).get("pressed", "button")
                    pins.append(Pin(int(b["gpio"]), f"button {act}", words(act)))
            except (ValueError, KeyError, TypeError, AttributeError):
                pass
            continue
        for k, v in re.findall(r"(?:^|,)\s*(\w+)\s*=\s*(-?\d+)", s):
            if k in KEYS and int(v) >= 0:
                w, out = KEYS[k]
                pins.append(Pin(int(v), f"{k}={v}", frozenset({w}), out))
        for g, what in re.findall(r"(?:^|,)\s*(\d+)\s*=\s*(\w+)", s):
            if not re.fullmatch(r"\d+", what) and what not in KEYS:
                w = words(what)
                pins.append(Pin(int(g), f"{g}={what}", w, bool(w & {"LED"})))
    return pins


def sdkconfig(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        m = re.match(r"^(CONFIG_\w+)=(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip('"')
        m = re.match(r"^#\s*(CONFIG_\w+) is not set", line.strip())
        if m:
            out[m.group(1)] = "n"
    return out


def module_nets(text: str, ref: str) -> dict[int, str]:
    """GPIO -> net name on the module's pads, from a KiCad netlist."""
    nets = text[text.find("(nets"):]
    out: dict[int, str] = {}
    for m in re.finditer(r'\(net\s*\(code\s+"?\d+"?\)\s*\(name\s+"([^"]*)"\)(.*?)(?=\(net\s*\(code|\Z)', nets, re.S):
        for n in re.finditer(rf'\(ref\s+"{re.escape(ref)}"\)\s*\(pin\s+"[^"]*"\)\s*\(pinfunction\s+"([^"]*)"\)', m.group(2)):
            fn = re.sub(r"_\d+$", "", n.group(1))
            g = int(fn[2:]) if re.fullmatch(r"IO\d+", fn) else PADS.get(fn)
            if g is not None:
                out[g] = m.group(1)
    return out


def check_pins(pins: list[Pin], nets: dict[int, str] | None, cfg: dict[str, str],
               module: str) -> list[Finding]:
    f: list[Finding] = []
    psram = cfg.get("CONFIG_ESP32_SPIRAM_SUPPORT") == "y" or cfg.get("CONFIG_SPIRAM") == "y" \
        or "WROVER" in module.upper()
    wifi = any(k.startswith(("CONFIG_ESP32_WIFI_", "CONFIG_ESP_WIFI_")) and v != "n" for k, v in cfg.items()) \
        and cfg.get("CONFIG_ESP_WIFI_ENABLED") != "n"
    by_gpio: dict[int, list[Pin]] = {}
    for p in pins:
        by_gpio.setdefault(p.gpio, []).append(p)
        if p.gpio in FLASH_PINS:
            f.append(Finding("PIN-FLASH", f"{p.name} on GPIO{p.gpio}, the module's SPI flash"))
        if p.gpio in PSRAM_PINS and psram:
            f.append(Finding("PIN-FLASH", f"{p.name} on GPIO{p.gpio}, the PSRAM's ({module or 'PSRAM on'})"))
        if p.gpio in INPUT_ONLY and p.out:
            f.append(Finding("PIN-INPUT", f"{p.name} drives GPIO{p.gpio}, which is input only"))
        if p.gpio == 12:
            f.append(Finding("STRAP-12", f"{p.name} on GPIO12 (MTDI): pulled high at reset it selects 1.8 V flash and the module won't boot"))
        if p.adc == 2 and wifi:
            f.append(Finding("ADC2-WIFI", f"{p.name} reads ADC2 (GPIO{p.gpio}) with Wi-Fi on: ADC2 reads fail while Wi-Fi runs"))
        if nets is not None:
            net = nets.get(p.gpio)
            if net is None or net.startswith("unconnected-") or net in ("GND", "+3V3", "3V3"):
                f.append(Finding("PIN-NET", f"{p.name} on GPIO{p.gpio}, which the board leaves {'unconnected' if not net or net.startswith('unconnected-') else 'on ' + net}"))
            elif p.words and not p.words & words(net):
                f.append(Finding("PIN-NET", f"{p.name} on GPIO{p.gpio}, but the board's net there is {net}"))
    for g, ps in sorted(by_gpio.items()):
        for i, a in enumerate(ps):
            b = next((b for b in ps[i + 1:] if a.words and b.words and not a.words & b.words), None)
            if b:
                f.append(Finding("PIN-SHARED", f"GPIO{g} is both {a.name} and {b.name}"))
                break
    if nets:
        named = {p.gpio for p in pins}
        for g, net in sorted(nets.items()):
            if g not in named and g not in (1, 3) and not net.startswith("unconnected-") \
                    and net not in ("GND", "+3V3", "3V3"):
                f.append(Finding("BOARD-UNUSED", f"the board wires GPIO{g} to {net}; no firmware pin names it", False))
    return f


def num(s: str) -> int | None:
    s = s.strip()
    if not s:
        return None
    m = re.fullmatch(r"(0x[0-9a-fA-F]+|\d+)\s*([KkMm]?)", s)
    if not m:
        raise ValueError(f"not a size: {s}")
    v = int(m.group(1), 0)
    return v * {"": 1, "k": 1024, "m": 1 << 20}[m.group(2).lower()]


def check_partitions(text: str, cfg: dict[str, str], sizes: dict[str, int]) -> list[Finding]:
    f: list[Finding] = []
    table = int(cfg.get("CONFIG_PARTITION_TABLE_OFFSET", "0x8000"), 0)
    flash = FLASH_SIZES.get(cfg.get("CONFIG_ESPTOOLPY_FLASHSIZE", ""), None)
    rows = [r for r in csv.reader(line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#"))]
    at = table + 0x1000
    parts = []
    for r in rows:
        r = [c.strip() for c in r] + [""] * 5
        name, typ, sub, off, size = r[:5]
        try:
            o, s = num(off), num(size)
        except ValueError as e:
            f.append(Finding("PART-BAD", f"{name}: {e}"))
            continue
        if s is None:
            f.append(Finding("PART-BAD", f"{name} has no size"))
            continue
        align = 0x10000 if typ == "app" else 0x1000
        if o is None:
            o = (at + align - 1) // align * align
        elif o % align:
            f.append(Finding("PART-ALIGN", f"{name} at {o:#x} is not {align:#x}-aligned"))
        if o < table + 0x1000:
            f.append(Finding("PART-OVERLAP", f"{name} at {o:#x} overlaps the partition table at {table:#x}"))
        parts.append((name, typ, sub, o, s))
        at = o + s
    for i, (n, _, _, o, s) in enumerate(parts):
        for n2, _, _, o2, s2 in parts[i + 1:]:
            if o < o2 + s2 and o2 < o + s:
                f.append(Finding("PART-OVERLAP", f"{n} ({o:#x}+{s:#x}) overlaps {n2} ({o2:#x}+{s2:#x})"))
        if flash and o + s > flash:
            f.append(Finding("PART-FLASH", f"{n} ends at {o + s:#x}, past the {flash >> 20} MB flash"))
    names = [p[0] for p in parts]
    for n in sorted({n for n in names if names.count(n) > 1}):
        f.append(Finding("PART-BAD", f"two partitions named {n}"))
    if any(p[1] == "app" and p[2].startswith("ota_") for p in parts) and \
            not any(p[1] == "data" and p[2] == "ota" for p in parts):
        f.append(Finding("PART-OTA", "OTA app slots but no otadata partition: the bootloader can't switch slots"))
    if not any(p[1] == "app" for p in parts):
        f.append(Finding("PART-BAD", "no app partition"))
    slots = {p[0]: p[4] for p in parts}
    for n, img in sizes.items():
        if n in slots and img > slots[n]:
            f.append(Finding("PART-IMAGE", f"the {n} image is {img:#x} bytes, over its {slots[n]:#x} slot"))
        elif n not in slots:
            f.append(Finding("PART-IMAGE", f"an image for {n}, but no partition {n}"))
    return f


def lint(root: Path, spec: dict, files: dict[str, str] | None = None) -> list[Finding]:
    """The findings on the firmware under root; files: {path: text} read in
    place of root's (the score plants its faults so, without a copy)."""
    def text(p: str) -> str:
        if files and p in files:
            return files[p]
        return (root / p).read_text(encoding="utf-8", errors="replace")

    def read(key: str) -> str | None:
        return text(spec[key]) if spec.get(key) else None

    outputs = set(re.findall(r"gpio_set_level\s*\(\s*(\w+)", "\n".join(text(p) for p in spec.get("sources", []))))
    pins = [p for h in spec.get("headers", []) for p in header_pins(text(h), outputs)]
    k = spec.get("kconfig")
    if k:
        pins += kconfig_pins(text(k["file"]), k["when"])
    cfg = sdkconfig(read("sdkconfig") or "")
    net_text = read("netlist")
    nets = module_nets(net_text, spec.get("module", "U1")) if net_text else None
    if net_text and not nets:
        return [Finding("NETLIST", f"no pads of {spec.get('module', 'U1')} found in {spec['netlist']}")]
    module = ""
    if net_text:
        m = re.search(rf'\(ref\s+"{re.escape(spec.get("module", "U1"))}"\)\s*\(value\s+"([^"]*)"\)', net_text)
        module = m.group(1) if m else ""
    found = check_pins(pins, nets, cfg, module)
    part = read("partitions")
    if part is not None:
        sizes = json.loads(read("sizes") or "{}")
        found += check_partitions(part, cfg, {n: int(v) for n, v in sizes.items()})
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fw lint", description=__doc__.split("\n")[0])
    ap.add_argument("dir", nargs="?", default=".", type=Path)
    ap.add_argument("--spec", type=Path, help="the lint spec (default DIR/lint.json)")
    a = ap.parse_args(argv)
    spec_file = a.spec or a.dir / "lint.json"
    try:
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
        found = lint(a.dir, spec)
    except (OSError, ValueError, KeyError) as e:
        print(f"fw lint: {e}", file=sys.stderr)
        return 2
    for x in found:
        print(x)
    errs = sum(x.error for x in found)
    print(f"fw lint: {errs} error(s), {len(found) - errs} note(s)")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
