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

    "pinmap": {"board": "../boards/x/kicad/x.kicad_sch",   (or a list; paths
                                                            from PROJECT)
               "sources": ["main/board.h", "patches/*.patch"],
               "nets": {"ACTRLS_TOGGLE": "BTN_PLAY"},     (name -> its net)
               "when": "HIFI_LITE",     (Kconfig `default ... if COND` lines
                                         count only when COND names it)
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


def gpio_of(pinfunction: str) -> int | None:
    """GPIO from a symbol's pin name: IO23, GPIO23, IO23_37 (EasyEDA's pin
    number suffix), IO0/BOOT, SENSOR_VP, U0TXD; None for power and NC."""
    for part in re.split(r"[/,]", pinfunction.upper()):
        part = part.strip()
        m = re.match(r"^(?:GPIO|IO)(\d+)(?:_\d+)?$", part)
        if m:
            return int(m.group(1))
        base = re.sub(r"_\d+$", "", part)
        for name in (part, base):
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
    that tie a net to a supply or to ground."""

    def __init__(self, path: Path, ref: str, part: str, gpios: dict[int, str | None], psram: bool,
                 pulls: dict[str, list[tuple[str, str]]] | None = None):
        self.path, self.ref, self.part, self.gpios, self.psram = path, ref, part, gpios, psram
        self.pulls = pulls or {}


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
    res_nets: dict[str, list[str | None]] = {}
    for nets in kids(root, "nets"):
        for n in kids(nets, "net"):
            name = net_name(val(n, "name"))
            for node in kids(n, "node"):
                ref = val(node, "ref")
                if re.fullmatch(r"R\d+", ref):
                    res_nets.setdefault(ref, []).append(name)
                value, fp = comps.get(ref, ("", ""))
                if (want_ref and ref != want_ref) or (not want_ref and not is_esp32(value, fp)):
                    continue
                g = gpio_of(val(node, "pinfunction"))
                if g is None:
                    continue
                found.setdefault(ref, {})[g] = name
    pulls: dict[str, list[tuple[str, str]]] = {}
    for ref, ns in sorted(res_nets.items()):
        if len(ns) != 2 or None in ns:
            continue
        for a, b in (ns, ns[::-1]):
            to = "up" if SUPPLY.fullmatch(b) else "down" if GROUND.fullmatch(b) else None
            if to and not (SUPPLY.fullmatch(a) or GROUND.fullmatch(a)):
                pulls.setdefault(a, []).append((ref, to))
    return [Board(path, r, comps.get(r, ("", ""))[0], g, has_psram(*comps.get(r, ("", ""))), pulls)
            for r, g in sorted(found.items())]


def from_layout(path: Path, text: str, want_ref: str | None) -> list[Board]:
    root = sexp(text)
    out = []
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
            g = gpio_of(fn) if fn else (MODULE_PADS.get(int(num)) if table and num.isdigit() else None)
            if g is None:
                continue
            net = kids(pad, "net")
            nm = net[0][-1] if net and isinstance(net[0][-1], str) else ""
            nm = "" if nm.isdigit() and len(net[0]) == 2 else nm  # (net 0) is no net
            gpios[g] = net_name(nm)
        if gpios:
            out.append(Board(path, ref, value, gpios, has_psram(value, name)))
    return out


KICAD_TIMEOUT = int(os.environ.get("FW_KICAD_TIMEOUT", "180"))


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


def firmware_pins(project: Path, globs: list[str], when: str | None = None) -> list[dict]:
    """[{"gpio", "name", "where"}] read from the sources. With when (a
    Kconfig symbol), a line that ends in `if COND` counts only when COND
    names it, so one Kconfig carrying several boards' defaults gives one
    board's pins."""
    out = []

    def add(g, name, where):
        if 0 <= g <= 48:
            out.append({"gpio": g, "name": name, "where": where})

    for where, line in source_lines(project, globs):
        cond = re.search(r"\sif\s+([^\"]+?)\s*$", line)
        if when and cond and not re.search(rf"\b{re.escape(when)}\b", cond.group(1)):
            continue
        m = re.match(r"\s*#\s*define\s+(\w+)\s+\(?\s*(?:GPIO_NUM_)?(\d+)\s*\)?\s*(?:/[/*].*)?$", line)
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
    return len(tokens(name) & tokens(net)) if net else 0


def check(project: Path, cfg: dict) -> dict:
    """The verdict: {"ok", "boards": [{file, ref, part, pins: [...], unused}], "error"}."""
    boards = cfg.get("board") or []
    boards = [boards] if isinstance(boards, str) else boards
    res: dict = {"ok": False, "boards": [], "error": ""}
    if not boards:
        res["error"] = "no board: pinmap.board names none"
        return res
    pins = firmware_pins(project, cfg.get("sources") or ["**/*.h", "**/*.c", "**/*.patch"], cfg.get("when"))
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
        classic = not re.search(r"ESP32-?(S\d|C\d|H\d|P\d)", esp.part.upper())
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
            if row["verdict"] == "pass" and g in STRAPPING:
                row["why"] = f"GPIO{g} is a strapping pin"
            used.add(g)
            ok &= row["verdict"] == "pass"
            rows.append(row)
        unused = {g: n for g, n in sorted(esp.gpios.items())
                  if n and g not in used and g not in FLASH | CONSOLE and n not in ignore
                  and not re.fullmatch(r"(?:GND|\+?3V3|VCC|VDD.*)", n)}
        res["boards"].append({"file": str(path), "read": note, "ref": esp.ref, "part": esp.part,
                              "pins": rows, "unused": unused})
    res["ok"] = ok
    return res


def config(project: Path, target: str | None) -> dict:
    if target:
        t = check_all.targets().get(target)
        if t is None:
            raise SystemExit(f"fw pins: no target {target} in targets.json")
        cfg = dict(t.get("pins", {}).get("pinmap") or {})
    else:
        cfg = {}
    f = project / "fw.json"
    if f.is_file():
        cfg.update(json.loads(f.read_text(encoding="utf-8")).get("pinmap") or {})
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
