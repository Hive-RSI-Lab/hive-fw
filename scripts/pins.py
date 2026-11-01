"""fw pins: the firmware's pin map checked against the board's KiCad netlist.

    fw pins [PROJECT] [--target NAME] [--board FILE ...] [--source GLOB ...] [--json]

It reads the GPIOs the firmware assigns from its sources and the nets on the
ESP32's pins from the board, then checks each firmware pin. The pin passes
when, of the nets on the ESP32, the one whose name best matches the
firmware's name for the pin (PIN_AMP_SDZ, squeezelite's bck=, a button's
ACTRLS_VOLUP) is the net on that GPIO. It fails when that best match sits on
another GPIO, when the GPIO isn't connected, when no net matches the name, or
when the pin is one the module's flash or PSRAM uses. Nets on the ESP32 that
the firmware never touches are listed as notes.

Where to look comes from the project's fw.json, or from targets.json for a
target (--target, with PROJECT its files, default the target's own dir):
its "pinmap", else what fw constraints' "constraints" says of the same keys
(board, sources, when, ref, nets, ignore, sdkconfig):

    "pinmap": {"board": "../boards/x/kicad/x.kicad_sch",   (or a list; paths
                                                            from PROJECT)
               "sources": ["main/board.h", "patches/*.patch"],
               "nets": {"ACTRLS_TOGGLE": "BTN_PLAY"},     (name -> its net)
               "when": "HIFI_LITE",     (Kconfig `default ... if COND` lines
                                         count only when COND names it;
                                         without it, when COND holds in
                                         the board's sdkconfig: pinmap's
                                         sdkconfig, the project's, or the
                                         sdkconfig.defaults in a folder
                                         named for the board)
               "ignore": ["PIN_SPARE"]}

A board is a netlist (.net), a schematic (.kicad_sch, exported to a netlist
with kicad-cli; without kicad-cli its sibling .net is read) or a layout
(.kicad_pcb: the pads' nets, so the check covers what is fabbed). In sources,
only a patch's added lines count. Read from the sources:
  - #define NAME N (or GPIO_NUM_N) where NAME says PIN, GPIO or a signal
    (SDA, SCL, TX, ...), and #define NAME ADCn_CHANNEL_m;
  - squeezelite-esp32's config strings: dac_config's bck=, ws=, do=, di=,
    mck=, sda=, scl=; set_GPIO's 14=ir, 22=green; buttons' {"gpio":13 ...
    "ACTRLS_VOLUP"}.
Exit 0 when every pin passes, 1 when one fails, 2 when it couldn't run (no
board, no ESP32 on it, no pins found). It reads files only (and runs
kicad-cli for a schematic), so hwde's side can run it on every board change.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_all  # noqa: E402

# ---- ESP32 pin names -> GPIO --------------------------------------------

CHIP_NAMES = {
    "SENSOR_VP": 36, "SENSOR_CAPP": 37, "SENSOR_CAPN": 38, "SENSOR_VN": 39,
    "VDET_1": 34, "VDET_2": 35, "32K_XP": 32, "32K_XN": 33,
    "MTMS": 14, "MTDI": 12, "MTCK": 13, "MTDO": 15,
    "U0TXD": 1, "TXD0": 1, "TXD": 1, "U0RXD": 3, "RXD0": 3, "RXD": 3,
    "SD_DATA_2": 9, "SD_DATA_3": 10, "SD_CMD": 11, "SD_CLK": 6, "SD_DATA_0": 7, "SD_DATA_1": 8,
    "SD2": 9, "SD3": 10, "CMD": 11, "CLK": 6, "SD0": 7, "SD1": 8,
}
# ESP32-WROOM-32 / WROVER pads 1..39, for a layout whose pads carry no pin names.
MODULE_PADS = {
    1: None, 2: None, 3: None, 4: 36, 5: 39, 6: 34, 7: 35, 8: 32, 9: 33, 10: 25,
    11: 26, 12: 27, 13: 14, 14: 12, 15: None, 16: 13, 17: 9, 18: 10, 19: 11, 20: 6,
    21: 7, 22: 8, 23: 15, 24: 2, 25: 0, 26: 4, 27: 16, 28: 17, 29: 5, 30: 18,
    31: 19, 32: None, 33: 21, 34: 3, 35: 1, 36: 22, 37: 23, 38: None, 39: None,
}
ADC = {1: {0: 36, 1: 37, 2: 38, 3: 39, 4: 32, 5: 33, 6: 34, 7: 35},
       2: {0: 4, 1: 0, 2: 2, 3: 15, 4: 13, 5: 12, 6: 14, 7: 27, 8: 25, 9: 26}}
FLASH = {6, 7, 8, 9, 10, 11}
CONSOLE = {1, 3}  # UART0: the IDF's console and the flasher
PSRAM = {16, 17}  # a WROVER's PSRAM; free on a WROOM
STRAPPING = {0, 2, 5, 12, 15}
# The newer chips put UART0 and their strapping pins elsewhere (each chip's
# datasheet, "Strapping Pins" and the IO MUX table): an S3 module's TXD0/RXD0
# pins are GPIO43/44, not the classic ESP32's GPIO1/3.
CHIP_UART0 = {"S2": {"TXD0": 43, "RXD0": 44}, "S3": {"TXD0": 43, "RXD0": 44},
              "C3": {"TXD0": 21, "RXD0": 20}, "C6": {"TXD0": 16, "RXD0": 17}}
CHIP_STRAPPING = {"classic": STRAPPING, "S2": {0, 45, 46}, "S3": {0, 3, 45, 46},
                  "C3": {2, 8, 9}, "C6": {8, 9, 15}}


def chip_of(*names: str) -> str:
    """'classic' (ESP32), or 'S2'/'S3'/'C3'/'C6'/... from a part or footprint name."""
    m = re.search(r"ESP32-?((?:S|C|H|P)\d)", " ".join(names).upper())
    return m.group(1) if m else "classic"


def gpio_of(pinfunction: str, chip: str = "classic") -> int | None:
    """GPIO from a symbol's pin name: IO23, GPIO23, IO23_37 (EasyEDA's pin
    number suffix), IO0/BOOT, SENSOR_VP, U0TXD; None for power and NC.
    chip ('classic', 'S3', ...) picks which GPIO a UART0 pin name means."""
    for part in re.split(r"[/,]", pinfunction.upper()):
        part = part.strip()
        m = re.match(r"^(?:GPIO|IO)(\d+)(?:_\d+)?$", part)
        if m:
            return int(m.group(1))
        base = re.sub(r"_\d+$", "", part)
        for name in (part, base):
            if chip != "classic":
                uart = CHIP_UART0.get(chip, {})
                key = {"U0TXD": "TXD0", "TXD": "TXD0", "U0RXD": "RXD0", "RXD": "RXD0"}.get(name, name)
                if key in uart:
                    return uart[key]
                if key in ("TXD0", "RXD0"):
                    return None   # a chip we have no table for: don't guess GPIO1/3
                continue          # the classic ESP32's other pad names don't apply
            if name in CHIP_NAMES:
                return CHIP_NAMES[name]
    return None


# ---- KiCad files ----------------------------------------------------------

def sexp(text: str) -> list:
    """KiCad's S-expressions as nested lists of strings."""
    tok = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')
    stack: list[list] = [[]]
    for m in tok.finditer(text):
        t = m.group(0)
        if t == "(":
            stack.append([])
        elif t == ")":
            done = stack.pop()
            stack[-1].append(done)
        elif t.startswith('"'):
            stack[-1].append(t[1:-1].replace('\\"', '"').replace("\\\\", "\\"))
        else:
            stack[-1].append(t)
    return stack[0][0] if stack[0] else []


def kids(node: list, key: str) -> list[list]:
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == key]


def val(node: list, key: str, default: str = "") -> str:
    for c in kids(node, key):
        if len(c) > 1 and isinstance(c[1], str):
            return c[1]
    return default


def is_esp32(value: str, footprint: str) -> bool:
    return "ESP32" in (value + " " + footprint).upper()


def has_psram(value: str, footprint: str) -> bool:
    return "WROVER" in (value + " " + footprint).upper()


class Board:
    """The ESP32 on a board: ref, part, GPIO -> net (None: unconnected), and
    from a netlist, net -> [(resistor, "up" or "down")] for the resistors
    that tie a net to a supply or to ground; the board's other parts (ref ->
    value) and net -> [(ref, pin name)] for every part's pins."""

    def __init__(self, path: Path, ref: str, part: str, gpios: dict[int, str | None], psram: bool,
                 pulls: dict[str, list[tuple[str, str]]] | None = None,
                 parts: dict[str, str] | None = None, nets: dict[str, list[tuple[str, str]]] | None = None,
                 caps: dict | None = None, other: dict | None = None):
        self.path, self.ref, self.part, self.gpios, self.psram = path, ref, part, gpios, psram
        self.pulls = pulls or {}
        self.parts, self.nets = parts or {}, nets or {}
        # from a netlist only (netlist is False for a schematic or layout): net -> [(capacitor, value)]
        # to ground and the ESP32's other pins (EN, ...) -> net
        self.netlist = caps is not None
        self.caps, self.other = caps or {}, other or {}


SUPPLY = re.compile(r"\+?(?:3V3|3\.3V|VCC|VDD)\w*", re.I)
GROUND = re.compile(r"[AD]?GND\w*|VSS", re.I)


def net_name(name: str) -> str | None:
    """A net's name without its sheet path; None for KiCad's unconnected-(...)."""
    if not name or name.startswith("unconnected-"):
        return None
    return name.rsplit("/", 1)[-1] or name


def from_netlist(path: Path, text: str, want_ref: str | None) -> list[Board]:
    root = sexp(text)
    comps = {}
    for comps_node in kids(root, "components"):
        for c in kids(comps_node, "comp"):
            comps[val(c, "ref")] = (val(c, "value"), val(c, "footprint"))
    found: dict[str, dict[int, str | None]] = {}
    other: dict[str, dict[str, str | None]] = {}
    two_nets: dict[str, list[str | None]] = {}
    all_nets: dict[str, list[tuple[str, str]]] = {}
    for nets in kids(root, "nets"):
        for n in kids(nets, "net"):
            name = net_name(val(n, "name"))
            for node in kids(n, "node"):
                ref = val(node, "ref")
                if name:
                    all_nets.setdefault(name, []).append((ref, val(node, "pinfunction")))
                if re.fullmatch(r"[RC]\d+", ref):
                    two_nets.setdefault(ref, []).append(name)
                value, fp = comps.get(ref, ("", ""))
                if (want_ref and ref != want_ref) or (not want_ref and not is_esp32(value, fp)):
                    continue
                g = gpio_of(val(node, "pinfunction"), chip_of(value, fp))
                if g is None:
                    fn = re.sub(r"_\d+$", "", val(node, "pinfunction").upper())
                    if fn:
                        other.setdefault(ref, {})[fn] = name
                    continue
                found.setdefault(ref, {})[g] = name
    pulls: dict[str, list[tuple[str, str]]] = {}
    caps: dict[str, list[tuple[str, str]]] = {}
    for ref, ns in sorted(two_nets.items()):
        if len(ns) != 2 or None in ns:
            continue
        for a, b in (ns, ns[::-1]):
            if SUPPLY.fullmatch(a) or GROUND.fullmatch(a):
                continue
            if ref[0] == "R" and (SUPPLY.fullmatch(b) or GROUND.fullmatch(b)):
                pulls.setdefault(a, []).append((ref, "up" if SUPPLY.fullmatch(b) else "down"))
            elif ref[0] == "C" and GROUND.fullmatch(b):
                caps.setdefault(a, []).append((ref, comps.get(ref, ("", ""))[0]))
    parts = {r: v for r, (v, _) in comps.items()}
    return [Board(path, r, comps.get(r, ("", ""))[0], g, has_psram(*comps.get(r, ("", ""))), pulls, parts, all_nets,
                  caps, other.get(r, {}))
            for r, g in sorted(found.items())]


def from_layout(path: Path, text: str, want_ref: str | None) -> list[Board]:
    root = sexp(text)
    out = []
    parts: dict[str, str] = {}
    all_nets: dict[str, list[tuple[str, str]]] = {}
    for fp in kids(root, "footprint"):
        ref = value = ""
        for p in kids(fp, "property"):
            if len(p) > 2 and p[1] in ("Reference", "Value"):
                ref, value = (p[2], value) if p[1] == "Reference" else (ref, p[2])
        parts[ref] = value
        for pad in kids(fp, "pad"):
            net = kids(pad, "net")
            nm = net[0][-1] if net and isinstance(net[0][-1], str) else ""
            nm = net_name("" if nm.isdigit() and len(net[0]) == 2 else nm)  # (net 0) is no net
            if nm:
                all_nets.setdefault(nm, []).append((ref, val(pad, "pinfunction")))
    for fp in kids(root, "footprint"):
        ref = value = ""
        for p in kids(fp, "property"):
            if len(p) > 2 and p[1] == "Reference":
                ref = p[2]
            elif len(p) > 2 and p[1] == "Value":
                value = p[2]
        ref = ref or next((c[2] for c in kids(fp, "fp_text") if len(c) > 2 and c[1] == "reference"), "")
        name = fp[1] if len(fp) > 1 and isinstance(fp[1], str) else ""
        if (want_ref and ref != want_ref) or (not want_ref and not is_esp32(value, name)):
            continue
        gpios: dict[int, str | None] = {}
        table = re.search(r"WROOM-32|WROVER", (value + name).upper()) and not re.search(
            r"ESP32-?(S\d|C\d|H\d|P\d)", (value + name).upper())
        for pad in kids(fp, "pad"):
            num = pad[1] if len(pad) > 1 else ""
            fn = val(pad, "pinfunction")
            g = gpio_of(fn, chip_of(value, name)) if fn else (MODULE_PADS.get(int(num)) if table and num.isdigit() else None)
            if g is None:
                continue
            net = kids(pad, "net")
            nm = net[0][-1] if net and isinstance(net[0][-1], str) else ""
            nm = "" if nm.isdigit() and len(net[0]) == 2 else nm  # (net 0) is no net
            gpios[g] = net_name(nm)
        if gpios:
            out.append(Board(path, ref, value, gpios, has_psram(value, name), None, parts, all_nets))
    return out


KICAD_TIMEOUT = int(os.environ.get("FW_KICAD_TIMEOUT", "180"))


# ---- I2C I/O expanders: their port pins' nets and address straps ----------

# part -> its 7-bit address with every address pin low, and its pins A2..A0 in
# the address's low bits; the TCA9539 has two (A1, A0) from 0x74
EXPANDERS = [
    (r"[TP]CA9539", 0x74), (r"[TP]CAL?95[35]5", 0x20), (r"[TP]CAL?6416", 0x20),
    (r"[TP]CAL?9554A|PCF857[45]A", 0x38), (r"[TP]CAL?95[35]4|PCF857[45]|MCP230(?:08|17)", 0x20),
    (r"XL9535", 0x20), (r"AW9523", 0x58),
]


class Expander:
    """An I/O expander on a board: ref, part, bit -> net (P00..P17 are bits
    0..15, MCP230xx's GPA/GPB likewise, an 8-bit part's P0..P7 bits 0..7) and
    its address as strapped (None when a strap isn't tied to a rail)."""

    def __init__(self, ref: str, part: str, bits: dict[int, str | None], addr: int | None, straps: dict[str, str | None]):
        self.ref, self.part, self.bits, self.addr, self.straps = ref, part, bits, addr, straps


def expander_bit(pinfunction: str) -> int | None:
    f = re.sub(r"_\d+$", "", pinfunction.upper())
    m = re.fullmatch(r"(?:P|IO)([01])_?([0-7])", f) or re.fullmatch(r"GP([AB])([0-7])", f)
    if m:
        port = {"0": 0, "1": 1, "A": 0, "B": 1}[m.group(1)]
        return port * 8 + int(m.group(2))
    m = re.fullmatch(r"P([0-7])", f)
    return int(m.group(1)) if m else None


def read_expanders(path: Path) -> list[Expander]:
    """The I/O expanders on a board's netlist (a schematic's sibling .net);
    none from a layout, whose pads carry no pin names."""
    net = path if path.suffix == ".net" else path.with_suffix(".net")
    if path.suffix == ".kicad_pcb" or not net.is_file():
        return []
    root = sexp(net.read_text(encoding="utf-8"))
    parts = {}
    for comps_node in kids(root, "components"):
        for c in kids(comps_node, "comp"):
            v = val(c, "value")
            base = next((b for pat, b in EXPANDERS if re.search(pat, v.upper())), None)
            if base is not None:
                parts[val(c, "ref")] = (v, base)
    bits: dict[str, dict[int, str | None]] = {r: {} for r in parts}
    straps: dict[str, dict[str, str | None]] = {r: {} for r in parts}
    for nets in kids(root, "nets"):
        for n in kids(nets, "net"):
            name = net_name(val(n, "name"))
            for node in kids(n, "node"):
                ref = val(node, "ref")
                if ref not in parts:
                    continue
                fn = val(node, "pinfunction")
                b = expander_bit(fn)
                if b is not None:
                    bits[ref][b] = name
                elif re.fullmatch(r"A[0-2]", re.sub(r"_\d+$", "", fn.upper())):
                    straps[ref][re.sub(r"_\d+$", "", fn.upper())] = name
    out = []
    for ref, (v, base) in sorted(parts.items()):
        addr: int | None = base
        for a, nm in straps[ref].items():
            hi = bool(nm and SUPPLY.fullmatch(nm))
            if not hi and not (nm and GROUND.fullmatch(nm)):
                addr = None
                break
            addr |= hi << int(a[1])
        out.append(Expander(ref, v, bits[ref], addr if straps[ref] else None, straps[ref]))
    return out


def kicad_cli() -> str | None:
    if os.environ.get("KICAD_CLI"):
        return os.environ["KICAD_CLI"]
    if shutil.which("kicad-cli"):
        return "kicad-cli"
    tools = Path(os.environ.get("HIVE_TOOLS", "/hive/tools"))
    apps = sorted(tools.glob("kicad-*/AppRun"))
    return f"{apps[-1]} kicad-cli" if apps else None


def read_board(path: Path, ref: str | None = None) -> tuple[list[Board], str]:
    """The ESP32s on a board file, and a note on how it was read."""
    if path.suffix == ".kicad_pcb":
        return from_layout(path, path.read_text(encoding="utf-8"), ref), ""
    if path.suffix == ".kicad_sch":
        cli = kicad_cli()
        if cli:
            with tempfile.TemporaryDirectory(prefix="fw-pins-") as d:
                out = Path(d) / "board.net"
                try:
                    r = subprocess.run([*cli.split(), "sch", "export", "netlist", "--output", str(out), str(path)],
                                       capture_output=True, text=True, timeout=KICAD_TIMEOUT)
                    if r.returncode == 0 and out.is_file():
                        return from_netlist(path, out.read_text(encoding="utf-8"), ref), "exported with kicad-cli"
                    note = f"kicad-cli failed ({(r.stderr or r.stdout).strip()[-200:]})"
                except subprocess.TimeoutExpired:
                    note = f"kicad-cli took over {KICAD_TIMEOUT} s"
        else:
            note = "no kicad-cli"
        net = path.with_suffix(".net")
        if not net.is_file():
            raise FileNotFoundError(f"{path}: {note}, and no {net.name} beside it")
        return from_netlist(path, net.read_text(encoding="utf-8"), ref), f"{note}: read {net.name}, which may be older"
    return from_netlist(path, path.read_text(encoding="utf-8"), ref), ""


# ---- the firmware's pins --------------------------------------------------

SIGNALS = {"SDA", "SCL", "MOSI", "MISO", "SCK", "SCLK", "CS", "TX", "RX", "TXD", "RXD",
           "BCK", "BCLK", "WS", "LRCK", "MCLK", "MCK", "DOUT", "DIN", "SDZ", "LED"}
# squeezelite-esp32's key=gpio settings (dac_config, spdif_config, display_config, eth_config)
SQ_KEYS = {"bck", "ws", "do", "di", "mck", "sda", "scl", "mute", "reset", "cs", "dc", "back",
           "mdc", "mdio", "rst", "intr", "clk", "mosi", "miso"}
# set_GPIO's roles
SQ_ROLES = {"ir", "green", "red", "amp", "jack", "spkfault", "power"}


def source_lines(project: Path, globs: list[str]):
    for g in globs:
        for f in sorted(glob.glob(str(project / g), recursive=True)):
            p = Path(f)
            if not p.is_file():
                continue
            patch = p.suffix in (".patch", ".diff")
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if patch:
                    if not line.startswith("+") or line.startswith("+++"):
                        continue
                    line = line[1:]
                yield f"{p.relative_to(project) if p.is_relative_to(project) else p}:{i}", line


SKIP_DIRS = {"build", "managed_components", "node_modules", "upstream", "components"}


def kconfig_symbols(path: Path) -> dict[str, bool]:
    """An sdkconfig's bool symbols, CONFIG_ dropped: =y True, =n or
    `# CONFIG_X is not set` False."""
    out = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"\s*CONFIG_(\w+)=([yn])\s*$", line) or re.match(r"\s*#\s*CONFIG_(\w+) is not set", line)
        if m:
            out[m.group(1)] = m.lastindex == 2 and m.group(2) == "y"
    return out


def board_sdkconfig(project: Path, cfg: dict) -> Path | None:
    """The sdkconfig the firmware is built with for this board: cfg's
    sdkconfig, else the project's sdkconfig or sdkconfig.defaults, else the
    one sdkconfig.defaults in a folder named for the board (board/lite-main/
    for ../boards/lite-main/kicad/lite-main.kicad_pcb)."""
    for c in ([cfg["sdkconfig"]] if cfg.get("sdkconfig") else ["sdkconfig", "sdkconfig.defaults"]):
        if (project / c).is_file():
            return project / c
    if cfg.get("sdkconfig"):
        return None
    boards = cfg.get("board") or []
    names = set()
    for b in [boards] if isinstance(boards, str) else boards:
        bp = Path(b)
        names |= {bp.stem} | {x for x in bp.parent.parts if x not in ("..", ".", "/", "kicad", "boards", "board")}
    if not names:
        return None
    found = []
    for root, dirs, files in os.walk(project):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
        if "sdkconfig.defaults" in files and Path(root).name in names:
            found.append(Path(root) / "sdkconfig.defaults")
    return found[0] if len(found) == 1 else None


def cond_of(line: str) -> str | None:
    """COND of a Kconfig `default ... if COND` line."""
    m = re.search(r"\sif\s+([^\"]+?)\s*$", line)
    return m.group(1) if m else None


def cond_holds(cond: str, when) -> bool:
    """Whether a line `... if COND` counts. when is a symbol (COND must name
    it) or an sdkconfig's {symbol: bool} (COND must hold; a symbol it
    doesn't set is off, as in Kconfig). A COND this can't read counts."""
    if not when:
        return True
    if isinstance(when, str):
        return bool(re.search(rf"\b{re.escape(when)}\b", cond))
    expr = []
    for t in re.findall(r"\|\||&&|!=|!|\(|\)|\w+|\S", cond):
        if t in ("||", "&&", "!", "(", ")"):
            expr.append({"||": " or ", "&&": " and ", "!": " not "}.get(t, t))
        elif re.fullmatch(r"[A-Za-z_]\w*", t):
            expr.append(str(bool(when.get(t))))
        else:
            return True
    try:
        return bool(eval("".join(expr), {"__builtins__": {}}))
    except SyntaxError:
        return True


def resolve_when(project: Path, cfg: dict):
    """cfg's when, else the board's sdkconfig's symbols when it sets one
    that a source's `if COND` line names, else None (every line counts)."""
    if cfg.get("when"):
        return cfg["when"]
    sdk = board_sdkconfig(project, cfg)
    if sdk is None:
        return None
    syms = kconfig_symbols(sdk)
    for _, line in source_lines(project, cfg.get("sources") or ["**/*.h", "**/*.c", "**/*.patch"]):
        c = cond_of(line)
        if c and any(t in syms for t in re.findall(r"\w+", c)):
            return syms
    return None


def firmware_pins(project: Path, globs: list[str], when=None) -> list[dict]:
    """[{"gpio", "name", "where"}] read from the sources. With when (a
    Kconfig symbol, or an sdkconfig's symbols: cond_holds), a line that ends
    in `if COND` counts only when COND holds for it, so one Kconfig carrying
    several boards' defaults gives one board's pins."""
    out = []

    def add(g, name, where):
        if 0 <= g <= 48:
            out.append({"gpio": g, "name": name, "where": where})

    # FRONT_ADDR 0x27 makes FRONT_* the I2C device's own names: its bits aren't GPIOs
    sources = list(source_lines(project, globs))
    device_prefixes = tuple(sorted({m.group(1) + "_" for _, line in sources
                                    if (m := re.match(r"\s*#\s*define\s+(\w+?)_ADDR\w*\s+\(?\s*0x[0-9a-fA-F]+", line))}))
    for where, line in sources:
        cond = cond_of(line)
        if cond and not cond_holds(cond, when):
            continue
        m = re.match(r"\s*#\s*define\s+(\w+)\s+\(?\s*(?:GPIO_NUM_)?(\d+)\s*\)?\s*(?:/[/*].*)?$", line)
        if m and m.group(1).startswith(device_prefixes):
            continue  # a bit of an I2C device (an I/O expander), not a GPIO
        if m and (set(m.group(1).upper().split("_")) & ({"PIN", "GPIO", "IO"} | SIGNALS)):
            add(int(m.group(2)), m.group(1), where)
            continue
        m = re.match(r"\s*#\s*define\s+(\w+)\s+\(?\s*ADC(\d)_CHANNEL_(\d+)\b", line)
        if m:
            g = ADC.get(int(m.group(2)), {}).get(int(m.group(3)))
            if g is not None:
                add(g, m.group(1), where)
            continue
        s = line.replace('\\"', '"')
        for m in re.finditer(r'"([^"]*)"', s):
            body = m.group(1)
            for k in re.finditer(r"(?<![\w.])(\w+)=(\d+)\b", body):
                if k.group(1) in SQ_KEYS:
                    add(int(k.group(2)), k.group(1), where)
            if re.search(r"(?:^|,)mck(?:,|$)", body):
                add(0, "mck", where)
            for k in re.finditer(r"(?:^|,)\s*(\d+)=(\w+)", body):
                if k.group(2) in SQ_ROLES:
                    add(int(k.group(1)), k.group(2), where)
        parts = re.split(r'"gpio"\s*:\s*', s)
        for part in parts[1:]:
            g = re.match(r"(\d+)", part)
            act = re.search(r'"(ACTRLS_\w+)"', part)
            if g:
                add(int(g.group(1)), act.group(1) if act else "button", where)
    return out


# ---- matching names to nets ----------------------------------------------

ALIASES = {
    "BCK": "BCLK", "SCLK": "BCLK", "WS": "LRCK", "LRCLK": "LRCK", "WCLK": "LRCK",
    "DO": "DOUT", "SDOUT": "DOUT", "DSDIN": "DOUT", "DI": "DIN", "SDIN": "DIN", "ASDOUT": "DIN",
    "MCK": "MCLK", "GREEN": "LED", "RED": "LED", "VOLDOWN": "VOLDN", "VOLMINUS": "VOLDN",
    "VOLPLUS": "VOLUP", "TOGGLE": "PLAY", "PLAYPAUSE": "PLAY", "FAULT": "FAULTZ",
    "RST": "RESET", "TXD": "TX", "RXD": "RX", "U0TXD": "TX", "U0RXD": "RX",
}
STOP = {"PIN", "GPIO", "IO", "ACTRLS", "BTN", "BUTTON", "ADC", "SENSE", "DRV", "IN", "OUT",
        "NUM", "CH", "CHANNEL", "SIG", "N", "P"}


def tokens(name: str) -> set[str]:
    words = re.split(r"[^A-Za-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1_\2", name).upper())
    return {ALIASES.get(w, w) for w in words if w and ALIASES.get(w, w) not in STOP}


def match(name: str, net: str | None) -> int:
    if net and name.lower() == "clk":
        # a bare squeezelite clk= is an SPI clock: a serial-clock net beats ETH_CLK and the like
        t = tokens(net)
        return 2 if t & {"BCLK", "SCK"} else len(t & {"CLK"})
    return len(tokens(name) & tokens(net)) if net else 0


def check(project: Path, cfg: dict) -> dict:
    """The verdict: {"ok", "boards": [{file, ref, part, pins: [...], unused}], "error"}."""
    boards = cfg.get("board") or []
    boards = [boards] if isinstance(boards, str) else boards
    res: dict = {"ok": False, "boards": [], "error": ""}
    if not boards:
        res["error"] = "no board: pinmap.board names none"
        return res
    pins = firmware_pins(project, cfg.get("sources") or ["**/*.h", "**/*.c", "**/*.patch"], resolve_when(project, cfg))
    ignore = set(cfg.get("ignore") or [])
    pins = [p for p in pins if p["name"] not in ignore]
    if not pins:
        res["error"] = "no firmware pins found in " + ", ".join(cfg.get("sources") or ["the sources"])
        return res
    nets = cfg.get("nets") or {}
    ok = True
    for b in boards:
        path = (project / b).resolve()
        if not path.is_file():
            res["error"] = f"no board file {path}"
            return res
        try:
            found, note = read_board(path, cfg.get("ref"))
        except (OSError, FileNotFoundError) as e:
            res["error"] = str(e)
            return res
        if len(found) != 1:
            res["error"] = (f"{path.name}: {'no ESP32' if not found else 'more than one ESP32 (' + ', '.join(x.ref for x in found) + ')'}"
                            " found; pinmap.ref names the one")
            return res
        esp = found[0]
        # the flash and PSRAM pins below are the ESP32's; an S2/S3/C3's differ
        chip = chip_of(esp.part)
        classic = chip == "classic"
        strapping = CHIP_STRAPPING.get(chip, set())
        flash = FLASH if classic else set()
        rows = []
        used = set()
        for p in pins:
            g, name = p["gpio"], p["name"]
            have = esp.gpios.get(g, "absent")
            row = {**p, "net": have if have != "absent" else None, "verdict": "pass", "why": ""}
            want = nets.get(name)
            if g in flash:
                row["verdict"], row["why"] = "fail", f"GPIO{g} is the module's flash"
            elif g in PSRAM and esp.psram and classic:
                row["verdict"], row["why"] = "fail", f"GPIO{g} is the {esp.part}'s PSRAM"
            elif have == "absent":
                row["verdict"], row["why"] = "fail", f"{esp.ref} has no GPIO{g} pin"
            elif have is None:
                row["verdict"], row["why"] = "fail", f"GPIO{g} isn't connected on the board"
            elif want:
                want = net_name(want)
                on = [x for x, n in esp.gpios.items() if n == want]
                if have != want:
                    row["verdict"] = "fail"
                    row["why"] = f"{want} is on {'GPIO' + str(on[0]) if on else 'no ESP32 pin'}"
            else:
                scores = {x: match(name, n) for x, n in esp.gpios.items()}
                best = max(scores.values())
                if best == 0:
                    row["verdict"] = "fail"
                    row["why"] = f"no net on {esp.ref} matches the name (pinmap.nets names its net)"
                elif scores[g] < best:
                    alt = [x for x, s in scores.items() if s == best]
                    row["verdict"] = "fail"
                    row["why"] = "the board has " + ", ".join(f"{esp.gpios[x]} on GPIO{x}" for x in sorted(alt))
            if row["verdict"] == "pass" and g in strapping:
                row["why"] = f"GPIO{g} is a strapping pin"
            used.add(g)
            ok &= row["verdict"] == "pass"
            rows.append(row)
        unused = {g: n for g, n in sorted(esp.gpios.items())
                  if n and g not in used and g not in ((FLASH | CONSOLE) if classic else set())
                  and n not in ignore
                  and not re.fullmatch(r"(?:GND|\+?3V3|VCC|VDD.*)", n)}
        res["boards"].append({"file": str(path), "read": note, "ref": esp.ref, "part": esp.part,
                              "pins": rows, "unused": unused})
    res["ok"] = ok
    return res


SHARED = ("board", "sources", "when", "ref", "nets", "ignore", "sdkconfig")


def config(project: Path, target: str | None) -> dict:
    """Where to look: a target's pins, then the project's fw.json over them.
    In each, "constraints" (fw constraints' config) gives the keys the two
    share (SHARED) and "pinmap" wins over it, so a project that only says
    "constraints" can run fw pins alone (hwde's side does)."""
    srcs = []
    if target:
        t = check_all.targets().get(target)
        if t is None:
            raise SystemExit(f"fw pins: no target {target} in targets.json")
        srcs.append(t.get("pins", {}))
    f = project / "fw.json"
    if f.is_file():
        srcs.append(json.loads(f.read_text(encoding="utf-8")))
    cfg = {}
    for src in srcs:
        cfg.update({k: v for k, v in (src.get("constraints") or {}).items() if k in SHARED})
        cfg.update(src.get("pinmap") or {})
    return cfg


def report(res: dict, out=None) -> None:
    out = out or sys.stdout
    if res["error"]:
        print(f"fw pins: can't check: {res['error']}", file=out)
        return
    for b in res["boards"]:
        print(f"{b['file']}: {b['ref']} {b['part']}{'  (' + b['read'] + ')' if b['read'] else ''}", file=out)
        for r in sorted(b["pins"], key=lambda r: (r["verdict"] == "pass", r["gpio"])):
            print(f"  {r['verdict'].upper() if r['verdict'] != 'pass' else 'pass'}  GPIO{r['gpio']:<3}"
                  f"{r['name']:<16} net {r['net'] or '-':<16} {r['why']}  ({r['where']})".rstrip(), file=out)
        for g, n in b["unused"].items():
            print(f"  note  GPIO{g:<3}{'':<16} net {n:<16} the firmware doesn't use it", file=out)
    print("pins: pass" if res["ok"] else "pins: FAIL", file=out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fw pins", description=__doc__.split("\n")[0])
    ap.add_argument("project", nargs="?", type=Path)
    ap.add_argument("--target", help="a target in targets.json: its pinmap, and its files unless PROJECT")
    ap.add_argument("--board", action="append", help="board file(s), instead of pinmap.board")
    ap.add_argument("--source", action="append", help="source glob(s), instead of pinmap.sources")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    project = a.project
    if project is None:
        if a.target:
            t = check_all.targets().get(a.target)
            project = check_all.target_dir(t) if t else Path(".")
        else:
            project = Path(".")
    project = project.resolve()
    cfg = config(project, a.target)
    if a.board:
        cfg["board"] = [str(Path(b).resolve()) for b in a.board]
    if a.source:
        cfg["sources"] = a.source
    res = check(project, cfg)
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        report(res)
    return 2 if res["error"] else (0 if res["ok"] else 1)


if __name__ == "__main__":
    sys.exit(main())
