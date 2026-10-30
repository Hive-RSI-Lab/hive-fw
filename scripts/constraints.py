"""fw constraints: what the firmware needs from the board, in one report.

    fw constraints [PROJECT] [--target NAME] [--board FILE ...] [--build DIR]
                   [--json] [--out FILE]

The board side (hwde) reads this before and while it designs: one list of
what the firmware takes for granted about the hardware, each with where in
the firmware it comes from. It reads files only, needs no IDF, and takes a
second, so it can run on every board change:

  - module: the chip, its lowest revision, flash size, PSRAM (and whether
    the firmware aborts without it), crystal, CPU clock, Wi-Fi/Bluetooth
    (an antenna keep-out, and the 3.3 V supply's peak current);
  - flash: the partition table against the flash size, and with a build,
    the app image against its partitions;
  - memory: with a build, the ELF's DRAM, IRAM, flash and PSRAM use;
  - pins: the GPIOs the firmware drives (fw pins reads them), each with the
    ESP32's rules for that pin: flash and PSRAM pins taken, input-only
    34-39, strapping levels at reset, ADC2 dead while Wi-Fi runs, the
    internal EMAC's fixed RMII pins, UART0 and GPIO0/EN for flashing;
  - i2c: the device addresses the firmware talks to (address straps) and
    the bus speed (pull-up size).

With a board (--board, or the config's board, a .net, .kicad_sch or
.kicad_pcb), it also checks the board's ESP32 part against the module
needs (flash, PSRAM, revision) and the pin map against the board's nets
(fw pins' check). Each finding is a need (what the board must provide) or
a problem (the firmware or the board breaks one); exit 0 with no problems,
1 with one, 2 when there was nothing to read.

Where to look comes from "constraints" in the project's fw.json, or in a
target's pins in targets.json (a target's files are PROJECT, default its
own dir); fw pins' "pinmap" fills in sources, when, board, ref, nets and
ignore:

    "constraints": {"sdkconfig": "board/core/sdkconfig.defaults",
                    "partitions": "board/core/partitions.csv",
                    "sources": ["components/**/*.h", "patches/*.patch"],
                    "when": "HIFI_PLUS",
                    "board": "../boards/core/kicad/core.net"}

Defaults: sdkconfig, else sdkconfig.defaults; the partition table the
sdkconfig names, beside it or in the project; sources **/*.h, **/*.c,
**/*.patch. The build is --build, else the project's build/ (or the
prepared tree's, fw build).
"""
from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fwlib  # noqa: E402
import check_all  # noqa: E402

import pins  # noqa: E402  (fw pins reads the firmware's GPIOs and the board's nets)

FLASH = {6, 7, 8, 9, 10, 11}
PSRAM = {16, 17}
INPUT_ONLY = {34, 35, 36, 37, 38, 39}
ADC2 = {0: 4, 1: 0, 2: 2, 3: 15, 4: 13, 5: 12, 6: 14, 7: 27, 8: 25, 9: 26}
STRAPS = {
    0: "GPIO0 must be high at reset (low enters download mode): whatever drives it must "
       "leave it pulled up until the firmware runs",
    2: "GPIO2 must be low or floating at reset, or serial download won't start",
    5: "GPIO5 sets SDIO slave timing at reset; leave it pulled up (its default)",
    12: "GPIO12 (MTDI) must be low at reset: high selects 1.8 V flash and a 3.3 V module won't boot",
    15: "GPIO15 (MTDO) high at reset prints the ROM boot log; low silences it",
}
# a strap the board's resistor holds the wrong way at reset: GPIO -> the bad pull
STRAP_BAD = {0: "down", 2: "up", 12: "up"}
# the classic ESP32's brown-out detector, level -> volts (the IDF's Kconfig)
BROWNOUT = {0: 2.43, 1: 2.48, 2: 2.58, 3: 2.62, 4: 2.67, 5: 2.70, 6: 2.77, 7: 2.80}
# the settings that turn PSRAM on (the rest use it)
PSRAM_SWITCHES = {"CONFIG_SPIRAM", "CONFIG_ESP32_SPIRAM_SUPPORT", "CONFIG_SPIRAM_SUPPORT"}
# the internal EMAC's RMII pins are fixed in silicon (the IO_MUX)
RMII = {19: "TXD0", 21: "TX_EN", 22: "TXD1", 25: "RXD0", 26: "RXD1", 27: "CRS_DV"}
RMII_PHYS = r"LAN87\w*|IP101\w*|RTL8201\w*|DP83848\w*|KSZ80\w*"
OUTPUT_WORDS = {"OUT", "DOUT", "DO", "TX", "TXD", "BCK", "BCLK", "WS", "LRCK", "MCK", "MCLK", "SCL", "SDA",
                "MDC", "MDIO", "LED", "GREEN", "RED", "RST", "RESET", "EN", "SDZ", "CS", "MOSI", "SCK",
                "SCLK", "MUTE", "AMP", "CLK"}


# ---- reading the firmware's files -----------------------------------------

def read_sdkconfig(path: Path | None) -> dict[str, str]:
    """CONFIG_X=value lines (quotes off); `# CONFIG_X is not set` reads as "n"."""
    out: dict[str, str] = {}
    if not path or not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^(CONFIG_\w+)=(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"')
            continue
        m = re.match(r"^#\s*(CONFIG_\w+) is not set", line.strip())
        if m:
            out[m.group(1)] = "n"
    return out


def size_of(s: str) -> int | None:
    s = s.strip()
    m = re.fullmatch(r"(0x[0-9a-fA-F]+|\d+)\s*([KkMm]?)", s)
    if not m:
        return None
    n = int(m.group(1), 0)
    return n * {"": 1, "k": 1024, "m": 1024 * 1024}[m.group(2).lower()]


def read_partitions(path: Path | None, table_offset: int = 0x8000) -> list[dict]:
    """The partition CSV as [{name, type, subtype, offset, size}], blank
    offsets placed as the IDF's gen_esp32part does (apps on 64 KB)."""
    rows: list[dict] = []
    if not path or not path.is_file():
        return rows
    nxt = table_offset + 0x1000
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        f = [x.strip() for x in line.split(",")] + [""] * 5
        name, typ, sub, off, size = f[:5]
        size_n = size_of(size)
        if size_n is None:
            continue
        align = 0x10000 if typ == "app" else 0x1000
        off_n = size_of(off) if off else None
        if off_n is None:
            off_n = (nxt + align - 1) // align * align
        rows.append({"name": name, "type": typ, "subtype": sub, "offset": off_n, "size": size_n})
        nxt = off_n + size_n
    return rows


def table_problems(table: list[dict], table_offset: int = 0x8000) -> list[tuple[str, str]]:
    """What the IDF's gen_esp32part or the bootloader refuses in a table."""
    out = []
    for i, p in enumerate(table):
        align = 0x10000 if p["type"] == "app" else 0x1000
        if p["offset"] % align:
            out.append(("PART-ALIGN", f"{p['name']} at {p['offset']:#x} is not {align:#x}-aligned"))
        if p["offset"] < table_offset + 0x1000:
            out.append(("PART-OVERLAP", f"{p['name']} at {p['offset']:#x} overlaps the partition table at {table_offset:#x}"))
        for q in table[i + 1:]:
            if p["offset"] < q["offset"] + q["size"] and q["offset"] < p["offset"] + p["size"]:
                out.append(("PART-OVERLAP", f"{p['name']} ({p['offset']:#x}+{p['size']:#x}) overlaps "
                            f"{q['name']} ({q['offset']:#x}+{q['size']:#x})"))
    names = [p["name"] for p in table]
    for n in sorted({n for n in names if names.count(n) > 1}):
        out.append(("PART-BAD", f"two partitions named {n}"))
    if any(p["type"] == "app" and p["subtype"].startswith("ota_") for p in table) and \
            not any(p["type"] == "data" and p["subtype"] == "ota" for p in table):
        out.append(("PART-OTA", "OTA app slots but no otadata partition: the bootloader can't switch slots"))
    if not any(p["type"] == "app" for p in table):
        out.append(("PART-BAD", "no app partition"))
    return out


def elf_sections(path: Path) -> dict[str, int]:
    """Section name -> size, for an ELF32 (the ESP32's), standard library only."""
    data = path.read_bytes()
    if data[:4] != b"\x7fELF" or data[4] != 1:
        raise ValueError(f"{path.name}: not a 32-bit ELF")
    end = "<" if data[5] == 1 else ">"
    shoff, = struct.unpack_from(end + "I", data, 0x20)
    shentsize, shnum, shstrndx = struct.unpack_from(end + "HHH", data, 0x2E)
    hdrs = [struct.unpack_from(end + "IIIIII", data, shoff + i * shentsize) for i in range(shnum)]
    stroff = hdrs[shstrndx][4]
    out: dict[str, int] = {}
    for name, _typ, _flags, _addr, _off, size in hdrs:
        nm = data[stroff + name:data.index(b"\0", stroff + name)].decode(errors="replace")
        if nm:
            out[nm] = out.get(nm, 0) + size
    return out


MEMORY = {  # what the ELF's sections land in, by section name prefix
    "dram": (".dram0",),
    "iram": (".iram0",),
    "flash_code": (".flash.text",),
    "flash_data": (".flash.rodata", ".flash.appdesc"),
    "psram": (".ext_ram",),
    "rtc": (".rtc",),
}


def memory(build: Path | None) -> dict:
    """The build's memory use from its ELF, and its app image's size."""
    if not build or not build.is_dir():
        return {"measured": False, "why": f"no build at {build}: fw build, then run this again" if build
                else "no build: fw build, then run this again"}
    desc = build / "project_description.json"
    elf = binf = None
    if desc.is_file():
        d = json.loads(desc.read_text(encoding="utf-8"))
        elf = build / d["app_elf"] if d.get("app_elf") else None
        binf = build / d["app_bin"] if d.get("app_bin") else None
    if not elf or not elf.is_file():
        elves = sorted(build.glob("*.elf"), key=lambda p: p.stat().st_size, reverse=True)
        elf = elves[0] if elves else None
    if not elf:
        return {"measured": False, "why": f"no app ELF in {build}"}
    if not binf or not binf.is_file():
        binf = elf.with_suffix(".bin")
    try:
        secs = elf_sections(elf)
    except (ValueError, struct.error, IndexError) as e:
        return {"measured": False, "why": str(e)}
    used = {k: sum(v for n, v in secs.items() if n.startswith(pre)) for k, pre in MEMORY.items()}
    return {"measured": True, "elf": str(elf), "used": used,
            "image": binf.stat().st_size if binf.is_file() else None}


def source_lines(project: Path, globs: list[str], when: str | None):
    """(where, line) over the sources, a patch's added lines only, a Kconfig
    `default ... if COND` line only when COND names `when`."""
    lines = pins.source_lines(project, globs)
    for where, line in lines:
        cond = re.search(r"\sif\s+([^\"]+?)\s*$", line)
        if when and cond and not re.search(rf"\b{re.escape(when)}\b", cond.group(1)):
            continue
        yield where, line


# ---- the report -----------------------------------------------------------

def config(project: Path, target: str | None) -> tuple[dict, dict]:
    """The firmware's pins (fw.json, a target's filling in) and its
    constraints config, with pinmap's keys filling in."""
    over = {}
    if target:
        t = check_all.targets().get(target)
        if t is None:
            raise SystemExit(f"fw constraints: no target {target} in targets.json")
        over = t.get("pins", {})
    fwpins = dict(over)
    f = project / "fw.json"
    own = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    fwpins.update(own)
    cfg = {}
    for src in (over, own):
        for k, v in (src.get("pinmap") or {}).items():
            cfg[k] = v
    for src in (over, own):
        cfg.update(src.get("constraints") or {})
    return fwpins, cfg


def module_part(part: str) -> dict:
    """What an Espressif module's name says: ESP32-WROVER-E-N8R8 is 8 MB
    flash, 8 MB PSRAM, chip v3 (the -E modules carry the ECO V3 die)."""
    up = part.upper()
    m = re.search(r"-N(\d+)(?:R(\d+))?\b", up)
    return {"flash_mb": int(m.group(1)) if m else None,
            "psram_mb": int(m.group(2)) if m and m.group(2) else (None if "WROVER" in up else 0),
            "psram": bool(m and m.group(2)) or "WROVER" in up,
            "min_rev": 3 if re.search(r"(WROOM-32|WROVER)-?[DU]?E\b|WROOM-32UE|-E-", up) else None}


def build_report(project: Path, fwpins: dict, cfg: dict, build: Path | None = None) -> dict:
    needs: list[dict] = []
    problems: list[dict] = []

    def need(about, text, where=""):
        needs.append({"about": about, "need": text, "from": where})

    def problem(code, about, text, where=""):
        problems.append({"code": code, "about": about, "problem": text, "from": where})

    target = fwpins.get("target", fwlib.DEFAULT_TARGET)
    sdk_path = None
    for c in ([cfg["sdkconfig"]] if cfg.get("sdkconfig") else ["sdkconfig", "sdkconfig.defaults"]):
        if (project / c).is_file():
            sdk_path = project / c
            break
    sdk = read_sdkconfig(sdk_path)
    sdk_from = (str(sdk_path.relative_to(project)) if sdk_path.is_relative_to(project) else str(sdk_path)) \
        if sdk_path else "the IDF's defaults"
    on = lambda k: sdk.get(k) == "y"  # noqa: E731

    # module
    mod: dict = {"chip": target}
    if sdk.get("CONFIG_ESP32_REV_MIN", "").isdigit():
        mod["min_rev"] = int(sdk["CONFIG_ESP32_REV_MIN"])
        if mod["min_rev"] > 0:
            need("module", f"an {target} of chip revision v{mod['min_rev']} or later", f"{sdk_from} CONFIG_ESP32_REV_MIN")
    flash = size_of(sdk.get("CONFIG_ESPTOOLPY_FLASHSIZE", "").replace("B", ""))
    mod["flash"] = flash
    if flash:
        need("module", f"{flash // (1 << 20)} MB of flash or more", f"{sdk_from} CONFIG_ESPTOOLPY_FLASHSIZE")
    # IDF 4's switch is ESP32_SPIRAM_SUPPORT (CONFIG_SPIRAM follows it), IDF 5's is SPIRAM
    psram = on("CONFIG_ESP32_SPIRAM_SUPPORT") if "CONFIG_ESP32_SPIRAM_SUPPORT" in sdk else on("CONFIG_SPIRAM")
    psram_optional = on("CONFIG_SPIRAM_IGNORE_NOTFOUND")
    mod["psram"] = "optional" if psram and psram_optional else psram
    if psram:
        sz = sdk.get("CONFIG_SPIRAM_SIZE", "-1")
        need("module", "PSRAM" + (f" of {int(sz) // (1 << 20)} MB" if sz.lstrip("-").isdigit() and int(sz) > 0 else "")
             + (" (used when found)" if psram_optional else ": the firmware aborts at boot without it")
             + "; on the classic ESP32 it takes GPIO16/17 (a WROVER)", f"{sdk_from} CONFIG_SPIRAM")
    for mhz in (40, 26):
        if on(f"CONFIG_ESP32_XTAL_FREQ_{mhz}"):
            mod["xtal_mhz"] = mhz
            need("module", f"a {mhz} MHz crystal (a module has it)", f"{sdk_from} CONFIG_ESP32_XTAL_FREQ_{mhz}")
    if sdk.get("CONFIG_ESP32_DEFAULT_CPU_FREQ_MHZ", "").isdigit():
        mod["cpu_mhz"] = int(sdk["CONFIG_ESP32_DEFAULT_CPU_FREQ_MHZ"])
    wifi = sdk.get("CONFIG_ESP32_WIFI_ENABLED", sdk.get("CONFIG_ESP_WIFI_ENABLED", "y")) != "n"
    bt = on("CONFIG_BT_ENABLED")
    mod["radio"] = {"wifi": wifi, "bt": bt}
    if wifi or bt:
        need("module", "an antenna keep-out: no copper, parts or ground under or beside the module's antenna "
             "(" + " and ".join(x for x, y in (("Wi-Fi", wifi), ("Bluetooth", bt)) if y) + " on)",
             f"{sdk_from} CONFIG_ESP32_WIFI_ENABLED/CONFIG_BT_ENABLED")
        need("power", "a 3.3 V supply good for 500 mA or more at the module, for the radio's transmit peaks "
             "(Espressif's module datasheets), with bulk capacitance at its supply pin", "radio on")
    else:
        need("power", "a 3.3 V supply good for the ESP32's run current (about 100 mA at 240 MHz, radio off)", "")
    if sdk.get("CONFIG_ESP32_BROWNOUT_DET_LVL", "").isdigit() and on("CONFIG_ESP32_BROWNOUT_DET"):
        lvl = int(sdk["CONFIG_ESP32_BROWNOUT_DET_LVL"])
        volts = f"{BROWNOUT[lvl]:.2f} V" if lvl in BROWNOUT else f"level {lvl}"
        need("power", f"the 3.3 V rail held above the brown-out threshold ({volts}) through transmit peaks, "
             "or the chip resets", f"{sdk_from} CONFIG_ESP32_BROWNOUT_DET_LVL")

    # flash
    table_path = None
    if cfg.get("partitions"):
        table_path = project / cfg["partitions"]
    elif on("CONFIG_PARTITION_TABLE_CUSTOM"):
        name = sdk.get("CONFIG_PARTITION_TABLE_CUSTOM_FILENAME", "partitions.csv")
        for d in ([sdk_path.parent] if sdk_path else []) + [project]:
            if (d / name).is_file():
                table_path = d / name
                break
        if table_path is None:
            near = sorted((sdk_path.parent if sdk_path else project).glob("partitions*.csv"))
            table_path = near[0] if near else None
    table_offset = size_of(sdk.get("CONFIG_PARTITION_TABLE_OFFSET", "0x8000")) or 0x8000
    table = read_partitions(table_path, table_offset)
    tbl_from = str(table_path.relative_to(project)) if table_path and table_path.is_relative_to(project) else str(table_path)
    fl: dict = {"size": flash, "table": tbl_from if table else None, "partitions": table}
    if table:
        endp = max(p["offset"] + p["size"] for p in table)
        fl["end"] = endp
        need("module", f"flash for the partition table: it ends at {endp:#x} ({endp / (1 << 20):.2f} MB)", tbl_from)
        if flash and endp > flash:
            problem("PART-FLASH", "flash", f"the partition table ends at {endp:#x}, past the {flash // (1 << 20)} MB flash", tbl_from)
        for code, text in table_problems(table, table_offset):
            problem(code, "flash", text, tbl_from)

    # memory
    mem = memory(build)
    if mem.get("measured") and mem.get("image") and table:
        apps = [p for p in table if p["type"] == "app" and p["subtype"].startswith("ota_")] or \
               [p for p in table if p["type"] == "app"]
        fl["image"] = mem["image"]
        for p in apps:
            free = p["size"] - mem["image"]
            p["free"] = free
            if free < 0:
                problem("PART-IMAGE", "flash", f"the app image ({mem['image']} B) is {-free} B over partition {p['name']} ({p['size']} B)",
                        mem["elf"])

    # images a build put in named partitions (the recovery app is not the main one)
    if cfg.get("images") and table:
        imgs = json.loads((project / cfg["images"]).read_text(encoding="utf-8"))
        slots = {p["name"]: p for p in table}
        for name, size in sorted(imgs.items()):
            if name not in slots:
                problem("PART-IMAGE", "flash", f"an image for {name}, but no partition {name}", cfg["images"])
            elif int(size) > slots[name]["size"]:
                problem("PART-IMAGE", "flash", f"the {name} image ({int(size)} B) is {int(size) - slots[name]['size']} B "
                        f"over its partition ({slots[name]['size']} B)", cfg["images"])
            else:
                slots[name]["free"] = slots[name]["size"] - int(size)

    # sdkconfig settings that put things in a PSRAM the firmware turned off
    if not psram:
        uses = sorted(k for k, v in sdk.items() if v == "y" and k not in PSRAM_SWITCHES
                      and re.search(r"SPIRAM|EXTERNAL_MEMORY|EXT_RAM", k))
        if uses:
            problem("SPIRAM-OFF", "module", "PSRAM support is off, but " + ", ".join(uses[:4])
                    + (f" and {len(uses) - 4} more" if len(uses) > 4 else "") + " put data in it", sdk_from)

    # pins and peripherals
    sources = cfg.get("sources") or ["**/*.h", "**/*.c", "**/*.patch"]
    when = cfg.get("when")
    ignore = set(cfg.get("ignore") or [])
    fw_pins = [p for p in pins.firmware_pins(project, sources, when) if p["name"] not in ignore]
    lines = list(source_lines(project, sources, when))
    eth = next(((w, m.group(1)) for w, s in lines for m in [re.search(rf"\bmodel=({RMII_PHYS})\b", s)] if m), None)
    if eth is None:  # the IDF enables its EMAC driver by default, so only the sources tell
        eth = next(((w, "RMII PHY") for w, s in lines if re.match(r"\s*#\s*define\s+\w*RMII\w*\s", s)), None)
    periph: dict = {}
    if eth:
        clk = None
        for g in (0, 16, 17):
            if any(p["gpio"] == g and re.search(r"ETH|RMII|REF|CLK", p["name"], re.I) for p in fw_pins):
                clk = g
        if sdk.get("CONFIG_ETH_RMII_CLK_IN_GPIO", "").isdigit():
            clk = int(sdk["CONFIG_ETH_RMII_CLK_IN_GPIO"])
        periph["rmii"] = {"phy": eth[1], "pins": {str(g): n for g, n in RMII.items()}, "clock": clk}
        need("pins", f"the {eth[1]} on the internal EMAC's fixed RMII pins: "
             + ", ".join(f"GPIO{g} {n}" for g, n in RMII.items())
             + f"; the 50 MHz reference clock on GPIO{clk if clk is not None else '0 (in), 16 or 17 (out)'}"
             + ("; a clock into GPIO0 must stay off through reset (GPIO0 is a strap)" if clk in (0, None) else ""),
             eth[0])
        for p in fw_pins:
            if p["gpio"] in RMII and not re.search(r"RMII|ETH|TX|RX|CRS", p["name"], re.I):
                problem("PIN-RMII", "pins", f"GPIO{p['gpio']} is the EMAC's RMII {RMII[p['gpio']]}, but the firmware also uses it "
                        f"for {p['name']}", p["where"])
    for w, s in lines:
        for m in re.finditer(r"\bADC2_CHANNEL_(\d+)\b", s):
            g = ADC2.get(int(m.group(1)))
            if wifi and g is not None:
                problem("ADC2-WIFI", "pins", f"ADC2 channel {m.group(1)} (GPIO{g}) reads nothing while Wi-Fi runs: "
                        "an analog input wants an ADC1 pin (GPIO32-39)", w)
    i2c = []
    for w, s in lines:
        m = re.match(r"\s*#\s*define\s+(\w+?)_ADDRS?\s+(.+?)\s*(?:/[/*](.*?))?(?:\*/)?\s*$", s)
        if m:
            addrs = re.findall(r"0x[0-9a-fA-F]{2}\b", m.group(2))
            if addrs:
                i2c.append({"device": re.sub(r"_I2C$", "", m.group(1)), "addr": addrs, "where": w, "about": (m.group(3) or "").strip()})
        for m in re.finditer(r'(?:sda=\d+,scl=\d+[^"]*?speed=(\d+))', s.replace('\\"', '"')):
            periph.setdefault("i2c_hz", int(m.group(1)))
            need("i2c", f"I2C pull-ups sized for {int(m.group(1)) // 1000} kHz on the bus's capacitance", w)
    for d in i2c:
        need("i2c", f"{d['device']} at I2C address {' or '.join(d['addr'])} (set by its address pins)"
             + (f": {d['about']}" if d["about"] else ""), d["where"])

    rows, straps = [], {}
    for p in sorted(fw_pins, key=lambda p: (p["gpio"], p["name"])):
        g, words = p["gpio"], pins.tokens(p["name"])
        rules = []
        if target == "esp32" and g in FLASH:
            problem("PIN-FLASH", "pins", f"GPIO{g} ({p['name']}) is the module's flash", p["where"])
        if target == "esp32" and psram and g in PSRAM:
            problem("PIN-FLASH", "pins", f"GPIO{g} ({p['name']}) is the PSRAM's on a WROVER", p["where"])
        if target == "esp32" and g in INPUT_ONLY:
            rules.append("input only, no pull-ups inside: the board pulls it")
            if words & OUTPUT_WORDS:
                problem("PIN-INPUT", "pins", f"GPIO{g} is input only, but {p['name']} looks like an output", p["where"])
        if target == "esp32" and g in STRAPS:
            rules.append("strapping pin")
            straps.setdefault(g, []).append(p)
        rows.append({**p, "rules": rules})
    for g, ps in sorted(straps.items()):
        names = sorted({p["name"] for p in ps})
        need("pins", f"{STRAPS[g]} (the firmware uses it for {', '.join(names)})", ps[0]["where"])
    by_gpio: dict[int, list[dict]] = {}
    for p in fw_pins:
        by_gpio.setdefault(p["gpio"], []).append(p)
    for g, ps in sorted(by_gpio.items()):
        clash = next(((a, b) for i, a in enumerate(ps) for b in ps[i + 1:]
                      if pins.tokens(a["name"]) and pins.tokens(b["name"])
                      and not pins.tokens(a["name"]) & pins.tokens(b["name"])), None)
        if clash:
            a, b = clash
            problem("PIN-SHARED", "pins", f"GPIO{g} is both {a['name']} ({a['where']}) and {b['name']}", b["where"])
    need("pins", "UART0 (GPIO1 TX, GPIO3 RX), EN and GPIO0 reachable for flashing and the boot log "
         "(a USB-UART with auto-reset, or a header)", "fw flash / fw monitor")
    # the same need can come from several places (one strap, two names)
    seen, uniq = set(), []
    for n in needs:
        if (n["about"], n["need"]) not in seen:
            seen.add((n["about"], n["need"]))
            uniq.append(n)
    needs = uniq

    # the board, when one is named
    boards = []
    if cfg.get("board"):
        files = [cfg["board"]] if isinstance(cfg["board"], str) else cfg["board"]
        for b in files:
            path = (project / b).resolve()
            if not path.is_file():
                problem("BOARD", "board", f"no board file {path}")
                continue
            try:
                found, note = pins.read_board(path, cfg.get("ref"))
            except (OSError, FileNotFoundError) as e:
                problem("BOARD", "board", str(e), str(path))
                continue
            if len(found) != 1:
                problem("BOARD", "board", f"{'no' if not found else 'more than one'} ESP32 on the board", str(path))
                continue
            esp = found[0]
            part = module_part(esp.part)
            bd = {"file": str(path), "ref": esp.ref, "part": esp.part, "module": part}
            if flash and part["flash_mb"] is not None and part["flash_mb"] << 20 < flash:
                problem("BOARD-FLASH", "board", f"{esp.ref} {esp.part} has {part['flash_mb']} MB of flash; the firmware needs "
                        f"{flash // (1 << 20)} MB", str(path))
            if psram and not psram_optional and not part["psram"]:
                problem("BOARD-PSRAM", "board", f"{esp.ref} {esp.part} has no PSRAM; the firmware needs it", str(path))
            if mod.get("min_rev", 0) >= 3 and part["min_rev"] is None:
                problem("BOARD-REV", "board", f"{esp.ref} {esp.part} may be older than chip v3; the firmware needs v{mod['min_rev']}",
                        str(path))
            if target == "esp32":
                for g, bad in STRAP_BAD.items():
                    net = esp.gpios.get(g)
                    rs = [r for r, to in esp.pulls.get(net, []) if to == bad] if net else []
                    if rs:
                        problem(f"STRAP-{g}", "board", f"{', '.join(rs)} pulls GPIO{g} ({net}) {bad}: {STRAPS[g]}", str(path))
            boards.append(bd)
        if files:
            res = pins.check(project, {**cfg, "board": files})
            for bd in boards:
                bd["pins"] = next((x for x in res["boards"] if x["file"] == bd.get("file")), None)
            if res["error"]:
                problem("BOARD", "board", f"pin map not checked: {res['error']}")
            for x in res["boards"]:
                for r in x["pins"]:
                    if r["verdict"] != "pass":
                        problem("PIN-NET", "board", f"GPIO{r['gpio']} {r['name']}: {r['why']}", r["where"])

    return {"format": "fw-constraints/1", "target": target, "idf": fwpins.get("idf"),
            "sdkconfig": sdk_from if sdk_path else None, "module": mod, "flash": fl, "memory": mem,
            "pins": rows,
            "peripherals": periph, "i2c": i2c, "boards": boards,
            "needs": needs, "problems": problems, "ok": not problems}


def report(res: dict, name: str, out=None) -> None:
    out = out or sys.stdout
    p = lambda *a: print(*a, file=out)  # noqa: E731
    p(f"fw constraints: {name} ({res['target']}, IDF {res['idf']}, sdkconfig {res['sdkconfig'] or 'none'})")
    p("needs from the board:")
    for n in res["needs"]:
        p(f"  {n['about']:<7} {n['need']}" + (f"  ({n['from']})" if n["from"] else ""))
    m = res["memory"]
    if m.get("measured"):
        p("memory (bytes): " + ", ".join(f"{k} {v}" for k, v in m["used"].items() if v)
          + (f"; app image {m['image']}" if m.get("image") else ""))
    else:
        p(f"memory: not measured ({m['why']})")
    for part in res["flash"]["partitions"]:
        if "free" in part:
            p(f"  {part['name']}: {part['size']} B, {part['free']} B free")
    p(f"pins the firmware uses: {len(res['pins'])}")
    for r in res["pins"]:
        p(f"  GPIO{r['gpio']:<3}{r['name']:<18}{'; '.join(r['rules']):<40} ({r['where']})".rstrip())
    for b in res["boards"]:
        pm = b.get("pins")
        p(f"board {b['file']}: {b['ref']} {b['part']}" + (f", pin map {'pass' if pm and not any(r['verdict'] != 'pass' for r in pm['pins']) else 'FAIL'}" if pm else ""))
    for x in res["problems"]:
        p(f"PROBLEM {x['code']}: {x['problem']}" + (f"  ({x['from']})" if x["from"] else ""))
    p("constraints: ok" if res["ok"] else f"constraints: {len(res['problems'])} problem(s)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fw constraints", description=__doc__.split("\n")[0])
    ap.add_argument("project", nargs="?", type=Path)
    ap.add_argument("--target", help="a target in targets.json: its config, and its files unless PROJECT")
    ap.add_argument("--board", action="append", help="board file(s), instead of the config's board")
    ap.add_argument("--build", type=Path, help="the build dir to measure (default: the project's)")
    ap.add_argument("--json", action="store_true", help="the report as JSON on stdout")
    ap.add_argument("--out", type=Path, help="also write the JSON report here")
    a = ap.parse_args(argv)
    project = a.project
    if project is None:
        t = check_all.targets().get(a.target) if a.target else None
        project = check_all.target_dir(t) if t else Path(".")
    project = project.resolve()
    fwpins, cfg = config(project, a.target)
    if a.board:
        cfg["board"] = [str(Path(b).resolve()) for b in a.board]
    build = a.build
    if build is None:
        try:
            build = fwlib.src_dir(project, fwpins) / "build"
        except (ValueError, KeyError):
            build = project / "build"
    res = build_report(project, fwpins, cfg, build.resolve())
    if a.out:
        a.out.write_text(json.dumps(res, indent=2) + "\n", encoding="utf-8")
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        report(res, a.target or project.name)
    if res["sdkconfig"] is None and not res["pins"] and not res["i2c"]:
        return 2
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
