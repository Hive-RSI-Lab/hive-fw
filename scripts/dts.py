"""fw dts: a Linux device tree's pin mux checked against the board's KiCad netlist.

    fw dts [PROJECT] [--dts FILE ...] [--board FILE ...] [--ref U1] [--include DIR ...] [--json]

dtc only catches a pad that doesn't exist. This catches a pad the schematic
moved and the DTS didn't: a board that boots with a dead PHY or LED. It reads
every pinctrl entry in the DTS (and the .dts/.dtsi files it #includes) and
the nets on the SoC's pins (its symbol's pin names are the pads: i.MX's
ENET1_TX_EN, an STM32's PA9, Rockchip's GPIO1_B2), then judges each entry:

  - PAD-NONE  the pad isn't a pin of the SoC (a typo, another package);
  - PAD-NC    the pad is muxed but not connected on the board;
  - PAD-NET   the function's name (ENET1_TDATA00) matches a net on another
              pad better than the net on its own (ETH_TXD0 sits on the pad
              the DTS gives ENET1_TDATA01), and that other pad is free or
              muxed to something it matches less: the schematic moved the
              signal and the DTS didn't;
  - note      the net's name says nothing either way (a GPIO, or nets named
              unlike the function), or a pad muxed in two groups (sleep
              states do that).

What an entry says by family:
  - i.MX (fsl,pins): MX6UL_PAD_<PAD>__<FUNCTION> (i.MX6/7, the pinfunc.h
    macros; the name alone says pad and function, so no header is needed)
    and MX8MM_IOMUXC_<PAD>_<FUNCTION> (i.MX8M, split at the SoC's pad
    names). GPIOn_IOm functions are judged by their group's name.
  - STM32 (pinmux = <STM32_PINMUX('A', 9, AF7)>), sunxi (pins = "PB8";
    function = "uart0") and Rockchip (rockchip,pins = <1 RK_PB2 1 &cfg>):
    the pad, judged by its group's name and function; these pass or note,
    and fail only PAD-NONE / PAD-NC, since an AF number names no signal.

Where to look comes from the project's fw.json "dts" (paths from PROJECT):

    "dts": {"files": ["linux/imx6ull-lc.dts"],          (or --dts)
            "include": ["linux/include"],              (#include search dirs)
            "board": "../boards/lc/kicad/lc.kicad_sch", (or --board; .net,
                                                        .kicad_sch, .kicad_pcb)
            "ref": "U1",                  (the SoC; default: the part whose
                                           pin names cover most of the pads)
            "nets": {"ENET1_TX_EN": "PHY_TXEN"},  (pad -> its net, when the
                                                   names don't say)
            "ignore": ["GPIO1_IO09"]}     (pads not judged)

Exit 0 when nothing fails, 1 when an entry fails, 2 when it couldn't run.
It reads files only (and runs kicad-cli for a schematic, as fw pins does).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pins  # noqa: E402

# ---- the device tree ------------------------------------------------------


def strip_comments(text: str) -> str:
    """C and C++ comments out, newlines kept (so line numbers hold)."""
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    return re.sub(r"/\*.*?\*/|//[^\n]*|\"(?:[^\"\\\n]|\\.)*\"", lambda m: m.group(0) if m.group(0).startswith('"') else blank(m),
                  text, flags=re.S)


INCLUDE = re.compile(r'^\s*(?:#include|/include/)\s*[<"]([^>"]+)[>"]', re.M)


def dts_files(path: Path, include: list[Path], seen: set | None = None) -> list[Path]:
    """The file and every local .dts/.dtsi it #includes, depth first, each
    once. <dt-bindings/...> headers and anything not found are skipped:
    the pin macros' names are all this reads."""
    seen = set() if seen is None else seen
    path = path.resolve()
    if path in seen:
        return []
    seen.add(path)
    out = [path]
    text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
    for inc in INCLUDE.findall(text):
        if not inc.endswith((".dts", ".dtsi")):
            continue
        for d in [path.parent, *include]:
            if (d / inc).is_file():
                out += dts_files(d / inc, include, seen)
                break
    return out


NODE = re.compile(r"(?:([\w-]+)\s*:\s*)?(&?[\w,.@+-]+)\s*\{")
IMX = re.compile(r"\bMX\w*?_PAD_(\w+?)__(\w+)")
IMX8 = re.compile(r"\bMX8\w*?_IOMUXC_(\w+)")
STM = re.compile(r"STM32_PINMUX\s*\(\s*'([A-Z])'\s*,\s*(\d+)\s*,\s*(\w+)\s*\)")
RK = re.compile(r"<\s*(\d+)\s+RK_P([A-D])(\d)\s+(\w+)")
STR = re.compile(r'"([^"]*)"')


def entries(path: Path, include: list[Path] | None = None) -> list[dict]:
    """Every pinctrl entry: {pad, function, family, group, where}. pad is
    None for an i.MX8M macro until it is split at the SoC's pads (raw)."""
    out = []
    for f in dts_files(path, include or []):
        text = strip_comments(f.read_text(encoding="utf-8", errors="replace"))
        text = re.sub(r"^\s*#[^\n]*", lambda m: " " * len(m.group(0)), text, flags=re.M)  # preprocessor lines
        stack: list[str] = []
        props: list[list[dict]] = [[]]   # each open node's properties (sunxi: pins + function)
        pos = 0
        for m in re.finditer(r"[{};]", text):
            seg, start = text[pos:m.start()], pos
            pos = m.end()
            lead = len(seg) - len(seg.lstrip())
            line = text.count("\n", 0, start + lead) + 1

            def at(off: int) -> str:
                return f"{f.name}:{text.count(chr(10), 0, start + off) + 1}"
            if m.group(0) == "{":
                h = NODE.search(seg + "{")
                stack.append((f"{h.group(1)}:{h.group(2)}" if h.group(1) else h.group(2)) if h else "/")
                props.append([])
                continue
            if m.group(0) == "}":
                node = props.pop() if len(props) > 1 else []
                fn = next((p["values"][0] for p in node if p["prop"] == "function" and p["values"]), "")
                for p in node:
                    if p["prop"] == "pins" and fn:
                        out += [{"pad": pad, "function": fn, "family": "sunxi", "group": p["group"],
                                 "where": p["where"]} for pad in p["values"] if re.fullmatch(r"P[A-Z]\d+", pad)]
                if stack:
                    stack.pop()
                continue
            pm = re.match(r"\s*(fsl,pins|pinmux|rockchip,pins|pins|function)\s*=", seg)
            if not pm:
                continue
            group, body, off = "/".join(stack), seg[pm.end():], pm.end()
            if pm.group(1) == "fsl,pins":
                for e in IMX.finditer(body):
                    out.append({"pad": e.group(1), "function": e.group(2), "family": "imx",
                                "group": group, "where": at(off + e.start())})
                for e in IMX8.finditer(body):
                    out.append({"pad": None, "raw": e.group(1), "function": "", "family": "imx",
                                "group": group, "where": at(off + e.start())})
            elif pm.group(1) == "pinmux":
                for e in STM.finditer(body):
                    out.append({"pad": f"P{e.group(1)}{e.group(2)}", "function": e.group(3), "family": "stm32",
                                "group": group, "where": at(off + e.start())})
            elif pm.group(1) == "rockchip,pins":
                for e in RK.finditer(body):
                    out.append({"pad": f"GPIO{e.group(1)}_{e.group(2)}{e.group(3)}", "function": "",
                                "family": "rockchip", "group": group, "where": at(off + e.start())})
            else:
                props[-1].append({"prop": pm.group(1), "values": STR.findall(body), "group": group,
                                  "where": f"{f.name}:{line}"})
    return out


# ---- the board -----------------------------------------------------------


def pin_names(fn: str, num: str) -> list[str]:
    """A symbol pin's names, upper case: EasyEDA's _<number> suffix off,
    each of IO1/ALT names split at '/'."""
    fn = fn.upper().strip()
    if num and fn.endswith("_" + num.upper()):
        fn = fn[: -len(num) - 1]
    return [p.strip() for p in re.split(r"[/,]", fn) if p.strip()]


def netlist_text(path: Path) -> tuple[str, str]:
    """A board file as netlist text (or layout text), and how it was read."""
    if path.suffix != ".kicad_sch":
        return path.read_text(encoding="utf-8"), ""
    cli = pins.kicad_cli()
    note = "no kicad-cli"
    if cli:
        with tempfile.TemporaryDirectory(prefix="fw-dts-") as d:
            out = Path(d) / "board.net"
            try:
                r = subprocess.run([*cli.split(), "sch", "export", "netlist", "--output", str(out), str(path)],
                                   capture_output=True, text=True, timeout=pins.KICAD_TIMEOUT)
                if r.returncode == 0 and out.is_file():
                    return out.read_text(encoding="utf-8"), "exported with kicad-cli"
                note = f"kicad-cli failed ({(r.stderr or r.stdout).strip()[-200:]})"
            except subprocess.TimeoutExpired:
                note = f"kicad-cli took over {pins.KICAD_TIMEOUT} s"
    net = path.with_suffix(".net")
    if not net.is_file():
        raise FileNotFoundError(f"{path}: {note}, and no {net.name} beside it")
    return net.read_text(encoding="utf-8"), f"{note}: read {net.name}, which may be older"


def board_pins(path: Path) -> tuple[dict[str, dict[str, str | None]], dict[str, str], str]:
    """{ref: {pad name: net or None}}, {ref: value}, and how it was read."""
    text, note = netlist_text(path)
    root = pins.sexp(text)
    out: dict[str, dict[str, str | None]] = {}
    values: dict[str, str] = {}
    if path.suffix == ".kicad_pcb":
        for fp in pins.kids(root, "footprint"):
            ref = value = ""
            for p in pins.kids(fp, "property"):
                if len(p) > 2 and p[1] == "Reference":
                    ref = p[2]
                elif len(p) > 2 and p[1] == "Value":
                    value = p[2]
            values[ref] = value
            for pad in pins.kids(fp, "pad"):
                num = pad[1] if len(pad) > 1 and isinstance(pad[1], str) else ""
                net = pins.kids(pad, "net")
                nm = net[0][-1] if net and isinstance(net[0][-1], str) else ""
                nm = "" if nm.isdigit() and len(net[0]) == 2 else nm
                for name in pin_names(pins.val(pad, "pinfunction"), num):
                    out.setdefault(ref, {})[name] = pins.net_name(nm)
        return out, values, note
    for comps in pins.kids(root, "components"):
        for c in pins.kids(comps, "comp"):
            values[pins.val(c, "ref")] = pins.val(c, "value")
    for nets in pins.kids(root, "nets"):
        for n in pins.kids(nets, "net"):
            name = pins.net_name(pins.val(n, "name"))
            for node in pins.kids(n, "node"):
                for p in pin_names(pins.val(node, "pinfunction"), pins.val(node, "pin")):
                    out.setdefault(pins.val(node, "ref"), {})[p] = name
    return out, values, note


# ---- names ----------------------------------------------------------------

ALIASES = {"ETH": "ENET", "EMAC": "ENET", "GMAC": "ENET", "FEC": "ENET", "CRSDV": "RXEN",
           "USDHC": "SD", "SDHC": "SD", "MMC": "SD", "SDMMC": "SD", "LPUART": "UART", "USART": "UART",
           "SERIAL": "UART", "LPI2C": "I2C", "ECSPI": "SPI", "LPSPI": "SPI", "QSPI": "SPI", "TXD": "TX",
           "RXD": "RX", "TXDATA": "TX", "RXDATA": "RX", "SCLK": "CLK", "SCK": "CLK", "CLOCK": "CLK", "LED": "LED", "LEDS": "LED"}
DROP = {"PAD", "DATA", "DCE", "DTE", "B", "N", "P", "IO", "GPIO", "PINCTRL", "GRP", "PINS", "PIN",
        "MUX", "DEFAULT", "SLEEP", "STATE", "AF", "ALT", "SIG", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"}


PAIRS = {("CRS", "DV"): "RXEN", ("RX", "DV"): "RXEN", ("REF", "CLK"): "REFCLK", ("TX", "EN"): "TXEN",
         ("RX", "EN"): "RXEN", ("RX", "ER"): "RXER"}


def tokens(name: str) -> set[str]:
    """A name's words for matching: ENET1_TDATA00 gives ENET, ENET1, TX,
    TXD0, ...; ETH_TXD0 gives ENET, TX, TXD0, ...: they meet on ENET, TX
    and TXD0, and ETH_TXD1 meets it on two, not three."""
    s = re.sub(r"([a-z])([A-Z])", r"\1_\2", name).upper()
    words = [w for w in re.split(r"[^A-Z0-9]+", s) if w]
    norm = []
    i = 0
    while i < len(words):
        w = words[i]
        m = re.fullmatch(r"([TR])DATA0*(\d+)", w)
        if m:
            w = f"{m.group(1)}XD{m.group(2)}"
        elif w in ("TX", "RX") and i + 1 < len(words) and re.fullmatch(r"DATA0*\d+", words[i + 1]):
            w = f"{w}D{int(words[i + 1][4:])}"
            i += 1
        norm.append(w)
        i += 1
    out = set()
    for w in norm:
        w = ALIASES.get(w, w)
        out.add(w)
        m = re.fullmatch(r"([A-Z]+?)0*(\d+)", w)
        if m:
            base = ALIASES.get(m.group(1), m.group(1))
            out |= {base, f"{base}{int(m.group(2))}"}
            if base in ("TX", "RX"):
                out.add(f"{base}D{int(m.group(2))}")
    for a, b in zip(norm, norm[1:]):
        b = re.sub(r"\d+$", "", b)
        if (a, b) in PAIRS:   # RX_EN is one signal, RXEN: its halves would meet RX_D1 or TX_EN
            out.add(PAIRS[(a, b)])
            out -= {a, b, ALIASES.get(a, a), ALIASES.get(b, b)}
    out |= {PAIRS[k] for k in PAIRS if "".join(k) in out}
    return {w for w in out if w not in DROP}


def score(name: str, net: str | None) -> int:
    return len(tokens(name) & tokens(net)) if net else 0


GPIO_FN = re.compile(r"^(?:GPIO\d*_IO\d+|GPIO\d*|GPIO_[A-Z]?\d+|ANALOG|AF\d+)$", re.I)


def hint(group: str) -> str:
    """A group's name for judging a GPIO by: its innermost node and label,
    without the pinctrl_/grp/-pins noise."""
    last = group.rsplit("/", 1)[-1] if group else ""
    return re.sub(r"@.*$", "", last)


# ---- the check ------------------------------------------------------------


def check(project: Path, cfg: dict) -> dict:
    res: dict = {"ok": False, "boards": [], "error": "", "entries": 0}
    files = cfg.get("files") or []
    files = [files] if isinstance(files, str) else files
    boards = cfg.get("board") or []
    boards = [boards] if isinstance(boards, str) else boards
    if not files:
        res["error"] = "no device tree: dts.files names none"
        return res
    if not boards:
        res["error"] = "no board: dts.board names none"
        return res
    include = [(project / d).resolve() for d in (cfg.get("include") or [])]
    ents = []
    for f in files:
        p = (project / f).resolve()
        if not p.is_file():
            res["error"] = f"no device tree {p}"
            return res
        ents += entries(p, include)
    ignore = {x.upper() for x in cfg.get("ignore") or []}
    forced = {k.upper(): v for k, v in (cfg.get("nets") or {}).items()}
    if not ents:
        res["error"] = "no pinctrl entries in " + ", ".join(files)
        return res
    ok = True
    for b in boards:
        path = (project / b).resolve()
        if not path.is_file():
            res["error"] = f"no board file {path}"
            return res
        try:
            parts, values, note = board_pins(path)
        except OSError as e:
            res["error"] = str(e)
            return res
        ref = cfg.get("ref")
        if ref and ref not in parts:
            res["error"] = f"{path.name}: no {ref} with named pins"
            return res
        if not ref:
            def cover(r):
                names = parts[r]
                return sum(1 for e in ents if (e["pad"] or "").upper() in names
                           or any((e.get("raw") or "").upper().startswith(n + "_") for n in names))
            best = max(parts, key=cover, default=None)
            if best is None or cover(best) == 0:
                res["error"] = f"{path.name}: no part has pins named like the DTS's pads; dts.ref names the SoC"
                return res
            ref = best
        soc = parts[ref]
        rows = []
        for e in ents:
            row = dict(e)
            if e["pad"] is None:   # i.MX8M: split at the longest pad name that prefixes it
                raw = e["raw"].upper()
                pad = max((n for n in soc if raw.startswith(n + "_")), key=len, default=None)
                if pad is None:
                    pad, _, fn = raw.partition("_")
                    row.update(pad=pad, function=fn)
                else:
                    row.update(pad=pad, function=raw[len(pad) + 1:])
            row["pad"] = row["pad"].upper()
            row.pop("raw", None)
            rows.append(row)
        muxed: dict[str, list[dict]] = {}
        for r in rows:
            muxed.setdefault(r["pad"], []).append(r)
        for r in rows:
            pad, fn = r["pad"], r["function"]
            r["verdict"], r["why"], r["code"] = "pass", "", ""
            if pad in ignore:
                r["verdict"], r["why"] = "note", "ignored (dts.ignore)"
                continue
            if pad not in soc:
                r["verdict"], r["code"], r["why"] = "fail", "PAD-NONE", f"{ref} has no pad {pad}"
                continue
            net = soc[pad]
            r["net"] = net
            if net is None:
                r["verdict"], r["code"], r["why"] = "fail", "PAD-NC", f"{pad} isn't connected on the board"
                continue
            if pad in forced:
                want = forced[pad]
                if net != want:
                    on = [p for p, n in soc.items() if n == want]
                    r["verdict"], r["code"] = "fail", "PAD-NET"
                    r["why"] = f"{want} is on {on[0] if on else 'no pad of ' + ref} (dts.nets)"
                continue
            named = r["family"] == "imx" and not GPIO_FN.match(fn)
            if not named:
                h = " ".join(x for x in (hint(r["group"]), fn if r["family"] == "sunxi" else "") if x)
                r["why"] = (f"{net} matches the group {h!r}" if score(h, net) else
                            f"by name only the group {h!r} could say, and {net} doesn't")
                r["verdict"] = "pass" if score(h, net) else "note"
                continue
            mine = score(fn, net)
            better = []
            for p, n in soc.items():
                if p == pad or n is None or n == net:
                    continue
                s = score(fn, n)
                if s <= mine:
                    continue
                # a pad the DTS gives a function that fits its own net at least as well isn't free
                taken = [o for o in muxed.get(p, []) if o is not r and
                         (score(o["function"], n) >= s or not (o["family"] == "imx" and not GPIO_FN.match(o["function"])))]
                if not taken:
                    better.append((s, p, n))
            if better:
                s, p, n = max(better)
                r["verdict"], r["code"] = "fail", "PAD-NET"
                r["why"] = f"{fn} on {pad} gets {net}; the board has {n} on {p}, which the DTS leaves free"
            elif mine == 0:
                r["verdict"], r["why"] = "note", f"{net} doesn't name {fn}, and no free pad's net does better"
        for pad, rs in muxed.items():
            groups = sorted({x["group"] for x in rs})
            if len(groups) > 1:
                for x in rs:
                    if x["verdict"] == "pass":
                        x["why"] = (x["why"] + "; " if x["why"] else "") + f"muxed in {len(groups)} groups"
        ok &= all(r["verdict"] != "fail" for r in rows)
        res["boards"].append({"file": str(path), "read": note, "ref": ref, "part": values.get(ref, ""),
                              "pins": rows})
        res["entries"] = len(rows)
    res["ok"] = ok
    return res


def config(project: Path) -> dict:
    f = project / "fw.json"
    return dict(json.loads(f.read_text(encoding="utf-8")).get("dts") or {}) if f.is_file() else {}


def report(res: dict, out=None) -> None:
    out = out or sys.stdout
    if res["error"]:
        print(f"fw dts: can't check: {res['error']}", file=out)
        return
    for b in res["boards"]:
        print(f"{b['file']}: {b['ref']} {b['part']}{'  (' + b['read'] + ')' if b['read'] else ''}", file=out)
        order = {"fail": 0, "note": 1, "pass": 2}
        for r in sorted(b["pins"], key=lambda r: (order[r["verdict"]], r["pad"])):
            tag = r["code"] or ("pass" if r["verdict"] == "pass" else "note")
            print(f"  {tag:<9}{r['pad']:<22}{r['function']:<20} net {r.get('net') or '-':<18} {r['why']}"
                  f"  ({r['where']})".rstrip(), file=out)
    print("dts: pass" if res["ok"] else "dts: FAIL", file=out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fw dts", description=__doc__.split("\n")[0])
    ap.add_argument("project", nargs="?", type=Path, default=Path("."))
    ap.add_argument("--dts", action="append", help="device tree file(s), instead of dts.files")
    ap.add_argument("--board", action="append", help="board file(s), instead of dts.board")
    ap.add_argument("--include", action="append", help="#include search dir(s), added to dts.include")
    ap.add_argument("--ref", help="the SoC's reference")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    project = a.project.resolve()
    cfg = config(project)
    if a.dts:
        cfg["files"] = [str(Path(x).resolve()) for x in a.dts]
    if a.board:
        cfg["board"] = [str(Path(x).resolve()) for x in a.board]
    if a.include:
        cfg["include"] = list(cfg.get("include") or []) + [str(Path(x).resolve()) for x in a.include]
    if a.ref:
        cfg["ref"] = a.ref
    res = check(project, cfg)
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        report(res)
    return 2 if res["error"] else (0 if res["ok"] else 1)


if __name__ == "__main__":
    sys.exit(main())
