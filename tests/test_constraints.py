import io
import json
import struct
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from tests import _util  # noqa: F401
import constraints

SDK = """CONFIG_IDF_TARGET="esp32"
CONFIG_ESPTOOLPY_FLASHSIZE_4MB=y
CONFIG_ESPTOOLPY_FLASHSIZE="4MB"
CONFIG_PARTITION_TABLE_CUSTOM=y
CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="parts.csv"
CONFIG_ESP32_REV_MIN_3=y
CONFIG_ESP32_REV_MIN=3
CONFIG_ESP32_XTAL_FREQ_40=y
CONFIG_ESP32_DEFAULT_CPU_FREQ_MHZ=240
CONFIG_SPIRAM=y
# CONFIG_SPIRAM_IGNORE_NOTFOUND is not set
CONFIG_BT_ENABLED=y
CONFIG_ESP32_BROWNOUT_DET=y
CONFIG_ESP32_BROWNOUT_DET_LVL=0
"""
PARTS = """# Name, Type, SubType, Offset, Size
nvs,      data, nvs,     0x9000,  0x4000,
otadata,  data, ota,     ,        0x2000,
phy_init, data, phy,     ,        0x1000,
factory,  app,  factory, ,        1M,
ota_0,    app,  ota_0,   ,        0x100000,
"""


def elf(sections: dict[str, int]) -> bytes:
    """A little-endian ELF32 with only section headers: name -> size."""
    names = [""] + list(sections) + [".shstrtab"]
    strtab = b"".join(n.encode() + b"\0" for n in names)
    offs, o = [], 0
    for n in names:
        offs.append(o)
        o += len(n) + 1
    shoff = 52 + len(strtab)
    head = b"\x7fELF" + bytes([1, 1, 1]) + bytes(9)
    head += struct.pack("<HHIIIIIHHHHHH", 2, 94, 1, 0, 0, shoff, 0, 52, 0, 0, 40, len(names), len(names) - 1)
    shdrs = b""
    for i, n in enumerate(names):
        size = len(strtab) if n == ".shstrtab" else sections.get(n, 0)
        off = 52 if n == ".shstrtab" else 0
        shdrs += struct.pack("<IIIIIIIIII", offs[i], 3 if n == ".shstrtab" else (1 if n else 0), 0, 0, off, size, 0, 0, 1, 0)
    return head + strtab + shdrs


class Constraints(unittest.TestCase):
    def setUp(self):
        self.p = Path(tempfile.mkdtemp())
        (self.p / "sdkconfig.defaults").write_text(SDK)
        (self.p / "parts.csv").write_text(PARTS)
        (self.p / "board.h").write_text(
            "#define DAC_ADDR 0x48 /* the DAC */\n"
            "#define EXP_ADDRS {0x20, 0x21}\n"
            "#define POT ADC2_CHANNEL_7\n"
            '#define CFG "model=LAN8720,mdc=23,mdio=18"\n'
            '#define I2C "sda=13,scl=14,speed=400000"\n')

    def run_report(self, cfg=None, build=None):
        return constraints.build_report(self.p, {"target": "esp32", "idf": "v4.4.8"},
                                        {"sources": ["*.h"], **(cfg or {})}, build)

    def needs(self, res, about=None):
        return [n["need"] for n in res["needs"] if about in (None, n["about"])]

    def test_module_needs_from_sdkconfig(self):
        res = self.run_report()
        mod = res["module"]
        self.assertEqual((mod["flash"], mod["psram"], mod["min_rev"], mod["xtal_mhz"]), (4 << 20, True, 3, 40))
        self.assertEqual(mod["radio"], {"wifi": True, "bt": True})
        text = " ".join(self.needs(res, "module"))
        self.assertIn("4 MB of flash", text)
        self.assertIn("aborts at boot without it", text)
        self.assertIn("revision v3", text)
        self.assertIn("antenna keep-out", text)
        self.assertTrue(any("500 mA" in n for n in self.needs(res, "power")))
        self.assertTrue(any("2.43 V" in n for n in self.needs(res, "power")))

    def test_psram_optional_and_radio_off(self):
        (self.p / "sdkconfig.defaults").write_text(SDK.replace("# CONFIG_SPIRAM_IGNORE_NOTFOUND is not set",
                                                               "CONFIG_SPIRAM_IGNORE_NOTFOUND=y")
                                                   .replace("CONFIG_BT_ENABLED=y", "")
                                                   + "# CONFIG_ESP32_WIFI_ENABLED is not set\n")
        res = self.run_report()
        self.assertEqual(res["module"]["psram"], "optional")
        self.assertFalse(any("antenna" in n for n in self.needs(res)))

    def test_partitions_placed_and_checked(self):
        res = self.run_report()
        parts = {p["name"]: p for p in res["flash"]["partitions"]}
        self.assertEqual(parts["phy_init"]["offset"], 0xF000)
        self.assertEqual(parts["factory"]["offset"], 0x10000)
        self.assertEqual(parts["ota_0"]["offset"], 0x110000)
        self.assertEqual(res["flash"]["end"], 0x210000)
        self.assertNotIn("flash", [x["about"] for x in res["problems"]])
        (self.p / "sdkconfig.defaults").write_text(SDK.replace('"4MB"', '"2MB"'))
        res = self.run_report()
        self.assertTrue(any("past the 2 MB flash" in x["problem"] for x in res["problems"]))

    def test_peripherals_from_sources(self):
        res = self.run_report()
        self.assertEqual(res["peripherals"]["rmii"]["phy"], "LAN8720")
        self.assertEqual(res["peripherals"]["i2c_hz"], 400000)
        self.assertEqual({d["device"]: d["addr"] for d in res["i2c"]}, {"DAC": ["0x48"], "EXP": ["0x20", "0x21"]})
        self.assertTrue(any("GPIO19 TXD0" in n for n in self.needs(res, "pins")))
        self.assertTrue(any("ADC2 channel 7 (GPIO27)" in x["problem"] for x in res["problems"]))
        self.assertTrue(any("UART0" in n for n in self.needs(res, "pins")))

    def test_when_picks_one_boards_lines(self):
        (self.p / "Kconfig.patch").write_text(
            '+\tdefault "model=LAN8720,mdc=23" if BOARD_A\n'
            '+\tdefault "model=IP101,mdc=23" if BOARD_B\n')
        (self.p / "board.h").write_text("")
        res = self.run_report({"sources": ["*.patch"], "when": "BOARD_B"})
        self.assertEqual(res["peripherals"]["rmii"]["phy"], "IP101")
        # the IDF's EMAC driver on by default isn't Ethernet on the board
        (self.p / "sdkconfig.defaults").write_text(SDK + "CONFIG_ETH_USE_ESP32_EMAC=y\n")
        self.assertNotIn("rmii", self.run_report({"sources": ["*.h"]})["peripherals"])
        (self.p / "board.h").write_text("#define PIN_RMII_TXD0 19\n")
        self.assertEqual(self.run_report({"sources": ["*.h"]})["peripherals"]["rmii"]["phy"], "RMII PHY")

    def test_without_when_the_boards_folder_sdkconfig_decides(self):
        (self.p / "sdkconfig.defaults").unlink(missing_ok=True)
        (self.p / "Kconfig.patch").write_text(
            '+\tdefault "model=LAN8720,mdc=23" if BOARD_A\n'
            '+\tdefault "model=IP101,mdc=23" if BOARD_B || BOARD_C\n')
        (self.p / "board" / "bee").mkdir(parents=True)
        (self.p / "board" / "bee" / "sdkconfig.defaults").write_text("CONFIG_BOARD_B=y\n# CONFIG_BOARD_A is not set\n")
        res = self.run_report({"sources": ["*.patch"], "board": "../boards/bee/kicad/bee.kicad_pcb"})
        self.assertEqual(res["sdkconfig"], "board/bee/sdkconfig.defaults")
        self.assertEqual(res["peripherals"]["rmii"]["phy"], "IP101")

    def test_memory_from_elf(self):
        b = self.p / "build"
        b.mkdir()
        (b / "app.elf").write_bytes(elf({".dram0.data": 100, ".dram0.bss": 50, ".iram0.text": 70,
                                          ".flash.text": 1000, ".flash.rodata": 300, ".ext_ram.bss": 9}))
        (b / "app.bin").write_bytes(b"\0" * 0x100100)
        (b / "project_description.json").write_text(json.dumps({"app_elf": "app.elf", "app_bin": "app.bin"}))
        res = self.run_report(build=b)
        m = res["memory"]
        self.assertTrue(m["measured"])
        self.assertEqual(m["used"]["dram"], 150)
        self.assertEqual(m["used"]["iram"], 70)
        self.assertEqual((m["used"]["flash_code"], m["used"]["flash_data"], m["used"]["psram"]), (1000, 300, 9))
        # the image goes in the ota slots, and it's 0x100 over ota_0
        self.assertTrue(any("over partition ota_0" in x["problem"] for x in res["problems"]))
        self.assertFalse(any("factory" in x["problem"] for x in res["problems"]))

    def test_no_build_is_not_measured(self):
        res = self.run_report(build=self.p / "build")
        self.assertFalse(res["memory"]["measured"])
        self.assertIn("fw build", res["memory"]["why"])

    def test_module_part_names(self):
        self.assertEqual(constraints.module_part("ESP32-WROVER-E-N8R8"),
                         {"flash_mb": 8, "psram_mb": 8, "psram": True, "min_rev": 3})
        w = constraints.module_part("ESP32-WROOM-32E-N4")
        self.assertEqual((w["flash_mb"], w["psram"], w["min_rev"]), (4, False, 3))
        self.assertIsNone(constraints.module_part("ESP32-WROOM-32")["min_rev"])

    def test_report_and_json_out(self):
        out = self.p / "c.json"
        with unittest.mock.patch("sys.stdout", io.StringIO()) as so:
            rc = constraints.main([str(self.p), "--out", str(out), "--build", str(self.p / "none")])
        self.assertEqual(rc, 1)  # the ADC2 problem
        self.assertIn("needs from the board:", so.getvalue())
        self.assertIn("PROBLEM ADC2-WIFI: ADC2", so.getvalue())
        self.assertEqual(json.loads(out.read_text())["format"], "fw-constraints/1")


class WithPins(unittest.TestCase):
    NET = """(export (version "E")
  (components
    (comp (ref "U1") (value "{part}") (footprint "RF_Module:ESP32-WROVER-E")))
  (nets
    (net (code "1") (name "/RMII_TXD0") (node (ref "U1") (pin "31") (pinfunction "IO19")))
    (net (code "2") (name "/I2S_BCK") (node (ref "U1") (pin "24") (pinfunction "IO2")))
    (net (code "3") (name "/SENSE") (node (ref "U1") (pin "6") (pinfunction "IO34")))))
"""

    def setUp(self):
        self.p = Path(tempfile.mkdtemp())
        (self.p / "sdkconfig").write_text(SDK.replace("CONFIG_ESP32_REV_MIN=3", "CONFIG_ESP32_REV_MIN=1"))
        (self.p / "parts.csv").write_text(PARTS)
        (self.p / "board.h").write_text("#define PIN_RMII_TXD0 19\n#define PIN_I2S_BCK 2\n#define PIN_LED 34\n")

    def run_report(self, part):
        (self.p / "b.net").write_text(self.NET.format(part=part))
        return constraints.build_report(self.p, {"target": "esp32"},
                                        {"sources": ["*.h"], "board": "b.net", "nets": {"PIN_LED": "SENSE"}})

    def test_pin_rules(self):
        res = self.run_report("ESP32-WROVER-E-N8R8")
        rows = {r["gpio"]: r for r in res["pins"]}
        self.assertIn("strapping pin", rows[2]["rules"])
        self.assertTrue(any("GPIO2 must be low" in n["need"] for n in res["needs"]))
        self.assertTrue(any("GPIO34 is input only" in x["problem"] for x in res["problems"]))

    def test_board_module_checked(self):
        res = self.run_report("ESP32-WROOM-32E-N2")
        text = " ".join(x["problem"] for x in res["problems"])
        self.assertIn("has 2 MB of flash", text)
        self.assertIn("has no PSRAM", text)
        self.assertEqual(res["boards"][0]["part"], "ESP32-WROOM-32E-N2")

    def test_shared_gpio(self):
        (self.p / "board.h").write_text("#define PIN_AMP_EN 19\n#define PIN_LED 19\n")
        res = self.run_report("ESP32-WROVER-E-N8R8")
        self.assertIn("PIN-SHARED", {x["code"] for x in res["problems"]})

    def test_a_strap_the_board_pulls_the_wrong_way(self):
        net = self.NET.replace('(name "/I2S_BCK")', '(name "/I2S_BCK") (node (ref "R1") (pin "1"))') \
            .replace(')))\n', ')\n    (net (code "4") (name "+3V3") (node (ref "R1") (pin "2")))))\n')
        (self.p / "b.net").write_text(net.format(part="ESP32-WROVER-E-N8R8"))
        res = constraints.build_report(self.p, {"target": "esp32"},
                                       {"sources": ["*.h"], "board": "b.net", "nets": {"PIN_LED": "SENSE"}})
        self.assertEqual([x["problem"] for x in res["problems"] if x["code"] == "STRAP-2"],
                         ["R1 pulls GPIO2 (I2S_BCK) up: " + constraints.STRAPS[2]])
        self.assertNotIn("STRAP-2", {x["code"] for x in self.run_report("ESP32-WROVER-E-N8R8")["problems"]})


class Expander(unittest.TestCase):
    """The firmware's I/O expander bits against the board's expander."""
    NET = """(export (version "E")
  (components
    (comp (ref "U1") (value "ESP32-WROVER-E-N8R8") (footprint "RF_Module:ESP32-WROVER-E"))
    (comp (ref "U2") (value "TCA9539") (footprint "TSSOP-24")))
  (nets
    (net (code "1") (name "/I2C_SDA") (node (ref "U1") (pin "16") (pinfunction "IO13")))
    (net (code "2") (name "/AMP_RST") (node (ref "U2") (pin "4") (pinfunction "P00_4")))
    (net (code "3") (name "/PVDD_EN") (node (ref "U2") (pin "5") (pinfunction "P01_5")))
    (net (code "4") (name "/AMP_FAULT") (node (ref "U2") (pin "13") (pinfunction "P10_13")))
    (net (code "5") (name "GND") (node (ref "U2") (pin "2") (pinfunction "A1_2")))
    (net (code "6") (name "+3V3") (node (ref "U2") (pin "21") (pinfunction "A0_21")))))
"""
    H = ("#define I2C_SDA 13\n#define EXP_ADDRS {0x74, 0x75}\n#define EXP_REG_IN0 0x00\n"
         "#define EXP_AMP_RST 0\n#define EXP_PVDD_EN 1\n#define EXP_AMP_FAULT 8 /* low = fault */\n"
         "#define EXP_CFG0 0x00\n#define EXP_CFG1 0xFF\n")

    def problems(self, h=None, net=None):
        p = Path(tempfile.mkdtemp())
        (p / "b.h").write_text(h or self.H)
        (p / "b.net").write_text(net or self.NET)
        res = constraints.build_report(p, {"target": "esp32"}, {"sources": ["*.h"], "board": "b.net"})
        return [(x["code"], x["problem"]) for x in res["problems"]], res

    def test_a_good_map(self):
        probs, res = self.problems()
        self.assertEqual(probs, [])
        self.assertEqual((res["boards"][0]["expander"]["ref"], res["boards"][0]["expander"]["addr"]), ("U2", 0x75))

    def test_a_bit_on_the_wrong_net(self):
        probs, _ = self.problems(self.H.replace("EXP_PVDD_EN 1", "EXP_PVDD_EN 0"))
        self.assertIn(("EXP-NET", "EXP_PVDD_EN is U2 P00 (AMP_RST), but the board has PVDD_EN on P01"), probs)
        probs, _ = self.problems(self.H.replace("EXP_PVDD_EN 1", "EXP_PVDD_EN 2"))
        self.assertEqual([c for c, _ in probs], ["EXP-NET"])

    def test_the_strapped_address_is_tried(self):
        probs, _ = self.problems(self.H.replace("{0x74, 0x75}", "{0x20, 0x74}"))
        self.assertEqual([c for c, _ in probs], ["EXP-ADDR"])
        self.assertIn("strapped to 0x75 (A0 +3V3, A1 GND)", probs[0][1])

    def test_directions(self):
        probs, _ = self.problems(self.H.replace("EXP_CFG0 0x00", "EXP_CFG0 0x02"))
        self.assertEqual([c for c, _ in probs], ["EXP-DIR"])
        self.assertIn("EXP_PVDD_EN (P01) is an output", probs[0][1])
        probs, _ = self.problems(self.H.replace("EXP_CFG1 0xFF", "EXP_CFG1 0xFE"))
        self.assertIn("EXP_AMP_FAULT (P10) is an input", probs[0][1])

    def test_no_expander_no_check(self):
        net = self.NET.replace('(value "TCA9539")', '(value "LM358")')
        self.assertEqual(self.problems(net=net)[0], [])


class AudioPath(unittest.TestCase):
    """MCLK on a pin that carries it, I2S data out into a data in, one
    address per I2C part on a bus, the firmware's addresses the board's."""
    NET = """(export (version "E")
  (components
    (comp (ref "U1") (value "ESP32-WROVER-E-N8R8"))
    (comp (ref "U2") (value "ES8388"))
    (comp (ref "U3") (value "{exp}")))
  (nets
    (net (code "1") (name "/I2S_MCLK") (node (ref "U1") (pin "25") (pinfunction "{mck_pin}")) (node (ref "U2") (pin "1") (pinfunction "MCLK_1")))
    (net (code "2") (name "/I2S_DOUT") (node (ref "U1") (pin "11") (pinfunction "IO26")) (node (ref "U2") (pin "6") (pinfunction "{codec_in}")))
    (net (code "3") (name "/I2S_DIN") (node (ref "U1") (pin "7") (pinfunction "IO35")) (node (ref "U2") (pin "8") (pinfunction "ASDOUT_8")))
    (net (code "4") (name "/I2C_SDA") (node (ref "U1") (pin "9") (pinfunction "IO33")) (node (ref "U2") (pin "27") (pinfunction "CDATA_27")) (node (ref "U3") (pin "23") (pinfunction "SDA")))
    (net (code "5") (name "/I2C_SCL") (node (ref "U1") (pin "8") (pinfunction "IO32")) (node (ref "U2") (pin "28") (pinfunction "CCLK_28")) (node (ref "U3") (pin "22") (pinfunction "SCL")))
    (net (code "6") (name "GND") (node (ref "U2") (pin "26") (pinfunction "{ce}")) (node (ref "U3") (pin "21") (pinfunction "A0")) (node (ref "U3") (pin "2") (pinfunction "A1")) (node (ref "U3") (pin "3") (pinfunction "A2")))
    (net (code "7") (name "+3V3") (node (ref "U2") (pin "2") (pinfunction "DVDD")))))
"""

    def problems(self, mck=0, h="#define ES8388_ADDR 0x10\n", **net):
        p = Path(tempfile.mkdtemp())
        (p / "board.h").write_text(f"#define PIN_I2S_MCLK {mck}\n#define PIN_I2S_DOUT 26\n#define PIN_I2S_DIN 35\n"
                                   "#define I2C_SDA 33\n#define I2C_SCL 32\n" + h)
        kw = {"exp": "PCA9539", "mck_pin": f"IO{mck}", "codec_in": "DSDIN_6", "ce": "CE_26", **net}
        (p / "b.net").write_text(self.NET.format(**kw))
        res = constraints.build_report(p, {"target": "esp32"}, {"sources": ["*.h"], "board": "b.net"})
        return [f"{x['code']}: {x['problem']}" for x in res["problems"] if x["code"].startswith(("I2S", "I2C"))]

    def test_a_good_audio_path(self):
        self.assertEqual(self.problems(), [])

    def test_mclk_off_gpio_0_1_3(self):
        got = self.problems(mck=5)
        self.assertEqual(sorted(x.split(":")[0] for x in got), ["I2S-MCLK", "I2S-MCLK"])
        self.assertIn("U2's MCLK is on GPIO5", " ".join(got))

    def test_data_out_into_data_out(self):
        got = self.problems(codec_in="ASDOUT_6")
        self.assertEqual(len(got), 1)
        self.assertIn("I2S-DIR: GPIO26 PIN_I2S_DOUT is the ESP32's I2S data out, but I2S_DOUT goes to U2 ES8388's ASDOUT", got[0])

    def test_two_parts_at_one_address(self):
        got = self.problems(exp="PCF8574")  # 0x20 with A2..A0 low; the ES8388 is 0x10
        self.assertEqual(got, [])
        got = self.problems(exp="TCA9555", h="#define ES8388_ADDR 0x10\n#define EXP_ADDR 0x20\n#define DSP_ADDR 0x20\n")
        self.assertEqual(got, ["I2C-CLASH: EXP and DSP are all at I2C address 0x20"])
        net = self.NET.replace('(pinfunction "A0")', '(pinfunction "x")')  # an unread strap: no address, no claim
        self.assertEqual(constraints.i2c_parts(_board(net.format(exp="TCA9555", mck_pin="IO0", codec_in="DSDIN_6",
                                                                     ce="CE_26")))[1]["addr"], None)

    def test_board_clash_and_firmware_address(self):
        net = self.NET.replace('(pinfunction "A0")', '(pinfunction "CE")')  # U3 a second ES8388, CE low
        got = self.problems_on(net.format(exp="ES8388", mck_pin="IO0", codec_in="DSDIN_6", ce="CE_26"))
        self.assertIn("I2C-CLASH: U2 ES8388 and U3 ES8388 are all at I2C address 0x10 on I2C_SDA: "
                      "one of them needs other address straps", got)
        net = self.NET.replace('(pinfunction "DVDD")', '(pinfunction "CE_26")')  # CE high: 0x11
        got = self.problems_on(net.format(exp="PCA9539", mck_pin="IO0", codec_in="DSDIN_6", ce="x"))
        self.assertEqual(got, ["I2C-ADDR: the firmware talks to ES8388 at 0x10, but U2 ES8388's address straps put it at 0x11"])

    def problems_on(self, net):
        p = Path(tempfile.mkdtemp())
        (p / "board.h").write_text("#define ES8388_ADDR 0x10\n#define PIN_I2S_MCLK 0\n")
        (p / "b.net").write_text(net)
        res = constraints.build_report(p, {"target": "esp32"}, {"sources": ["*.h"], "board": "b.net"})
        return [f"{x['code']}: {x['problem']}" for x in res["problems"] if x["code"].startswith(("I2S", "I2C"))]


def _board(net: str):
    p = Path(tempfile.mkdtemp()) / "b.net"
    p.write_text(net)
    return constraints.pins.read_board(p)[0][0]


class Partitions(unittest.TestCase):
    """The table rules fw lint (task 261030-030425) had, now the checker's."""
    T = "# n, t, s, o, size\nnvs, data, nvs, 0x9000, 0x4000,\notadata, data, ota, , 0x2000,\n" \
        "ota_0, app, ota_0, 0x10000, 1M,\nota_1, app, ota_1, , 1M,\n"

    def codes(self, csv, flash="4MB", images=None):
        p = Path(tempfile.mkdtemp())
        (p / "sdkconfig").write_text(f'CONFIG_ESPTOOLPY_FLASHSIZE="{flash}"\n')
        (p / "p.csv").write_text(csv)
        cfg = {"partitions": "p.csv", "sources": []}
        if images is not None:
            (p / "sizes.json").write_text(json.dumps(images))
            cfg["images"] = "sizes.json"
        res = constraints.build_report(p, {"target": "esp32"}, cfg, None)
        return sorted({x["code"] for x in res["problems"]})

    def test_a_good_table(self):
        self.assertEqual(self.codes(self.T), [])
        self.assertEqual(self.codes(self.T, images={"ota_0": 900000}), [])

    def test_each_rule(self):
        self.assertEqual(self.codes(self.T, "2MB"), ["PART-FLASH"])
        self.assertEqual(self.codes(self.T, images={"ota_0": 0x100001}), ["PART-IMAGE"])
        self.assertEqual(self.codes(self.T, images={"ota_9": 1}), ["PART-IMAGE"])
        self.assertEqual(self.codes(self.T.replace("0x9000", "0x8000")), ["PART-OVERLAP"])
        self.assertIn("PART-ALIGN", self.codes(self.T.replace("0x10000", "0x11000")))
        self.assertEqual(self.codes(self.T.replace("otadata, data, ota, , 0x2000,\n", "")), ["PART-OTA"])
        self.assertIn("PART-OVERLAP", self.codes(self.T.replace("0x4000", "0x8000")))
        self.assertIn("PART-BAD", self.codes(self.T + "nvs, data, nvs, , 0x1000,\n"))

    def test_psram_off_while_settings_use_it(self):
        p = Path(tempfile.mkdtemp())
        (p / "sdkconfig").write_text("# CONFIG_ESP32_SPIRAM_SUPPORT is not set\nCONFIG_SPIRAM=y\n"
                                     "CONFIG_BT_ALLOCATION_FROM_SPIRAM_FIRST=y\n")
        res = constraints.build_report(p, {"target": "esp32"}, {"sources": []}, None)
        self.assertEqual([x["code"] for x in res["problems"]], ["SPIRAM-OFF"])
        self.assertFalse(res["module"]["psram"])


class Mistakes(unittest.TestCase):
    """The clock, bus, partition, power and serial-port mistakes: each check
    quiet on a good board and firmware, and each firing on its mistake."""
    NET = """(export (version "E")
  (components
    (comp (ref "U1") (value "ESP32-WROVER-E-N8R8"))
    (comp (ref "U2") (value "CH340C"))
    (comp (ref "R1") (value "10k")) (comp (ref "C1") (value "1uF"))
    (comp (ref "R2") (value "2.2k")) (comp (ref "R3") (value "10k")))
  (nets
    (net (code "1") (name "/EN") (node (ref "U1") (pin "3") (pinfunction "EN")) (node (ref "R1") (pin "2"))
      (node (ref "C1") (pin "1")))
    (net (code "2") (name "+3V3") (node (ref "R1") (pin "1")) (node (ref "R2") (pin "1")) (node (ref "R3") (pin "1")))
    (net (code "3") (name "GND") (node (ref "C1") (pin "2")))
    (net (code "4") (name "/U0TXD") (node (ref "U1") (pin "35") (pinfunction "TXD0")) (node (ref "U2") (pin "3") (pinfunction "RXD")))
    (net (code "5") (name "/U0RXD") (node (ref "U1") (pin "34") (pinfunction "RXD0")) (node (ref "U2") (pin "2") (pinfunction "TXD")))
    (net (code "6") (name "/I2C_SDA") (node (ref "U1") (pin "9") (pinfunction "IO33")) (node (ref "R2") (pin "2")))
    (net (code "7") (name "/BTN_VOLUP") (node (ref "U1") (pin "16") (pinfunction "IO13")) (node (ref "R3") (pin "2")))
    (net (code "8") (name "/I2S_MCLK") (node (ref "U1") (pin "25") (pinfunction "IO0")))))
"""
    SDK = SDK + "CONFIG_ESPTOOLPY_FLASHFREQ_80M=y\nCONFIG_SPIRAM_SPEED_80M=y\nCONFIG_BT_CLASSIC_ENABLED=y\n" \
        "CONFIG_BTDM_CTRL_MODE_BR_EDR_ONLY=y\n"
    SRC = ('#define I2C_SDA 33\n#define ES8388_ADDR 0x10\n'
           '#define DAC "model=ES8388,i2c=16,mck=0"\n'
           '#define BTN "[{\\"gpio\\":13,\\"pull\\":false,\\"normal\\":{\\"pressed\\":\\"ACTRLS_VOLUP\\"}}]"\n')

    def codes(self, net=None, sdk=None, src=None, parts=None):
        p = Path(tempfile.mkdtemp())
        (p / "sdkconfig").write_text(sdk or self.SDK)
        (p / "parts.csv").write_text(parts or PARTS)
        (p / "board.h").write_text(src or self.SRC)
        (p / "b.net").write_text(net or self.NET)
        res = constraints.build_report(p, {"target": "esp32"}, {"sources": ["*.h"], "board": "b.net",
                                                                "nets": {"BTN": "BTN_VOLUP", "DAC": "I2S_MCLK"}})
        return sorted({x["code"] for x in res["problems"]})

    def test_a_good_board_and_firmware(self):
        self.assertEqual(self.codes(), [])

    def test_firmware_mistakes(self):
        self.assertEqual(self.codes(sdk=self.SDK.replace("FLASHFREQ_80M", "FLASHFREQ_40M")), ["CLOCK-SPI"])
        self.assertEqual(self.codes(sdk=self.SDK.replace("CONFIG_ESP32_XTAL_FREQ_40", "CONFIG_ESP32_XTAL_FREQ_26")),
                         ["CLOCK-XTAL"])
        self.assertEqual(self.codes(sdk=self.SDK.replace("BR_EDR_ONLY", "BLE_ONLY")), ["BT-CLASSIC"])
        self.assertEqual(self.codes(src=self.SRC.replace("0x10", "0x20")), ["I2C-ADDR"])
        self.assertEqual(self.codes(src=self.SRC.replace("i2c=16", "i2c=26")), ["I2C-ADDR"])
        self.assertIn("I2S-MCLK", self.codes(src=self.SRC.replace("mck=0", "mck=5")))
        self.assertEqual(self.codes(parts=PARTS.replace("nvs,", "cfg,")), ["PART-NVS"])
        self.assertEqual(self.codes(parts=PARTS.replace("0x4000", "0x2000")), ["PART-NVS"])
        self.assertEqual(self.codes(parts=PARTS.replace("0x2000", "0x1000")), ["PART-OTADATA"])

    def test_board_mistakes(self):
        drop = lambda ref, pin: self.NET.replace(f'(node (ref "{ref}") (pin "{pin}"))', "")  # noqa: E731
        self.assertEqual(self.codes(drop("C1", "2")), ["POWER-EN"])
        self.assertEqual(self.codes(drop("R1", "1")), ["POWER-EN"])
        self.assertEqual(self.codes(drop("R2", "1")), ["I2C-PULLUP"])
        self.assertEqual(self.codes(drop("R3", "1")), ["PIN-FLOAT"])
        self.assertEqual(self.codes(self.NET.replace('(pinfunction "RXD")', '(pinfunction "TXD_X")')
                                    .replace('(pinfunction "TXD")', '(pinfunction "RXD")')
                                    .replace("TXD_X", "TXD")), ["UART0"])
        self.assertEqual(self.codes(self.NET.replace('(node (ref "U2") (pin "3") (pinfunction "RXD"))', "")), ["UART0"])
        # the internal pull is enough for a button on an ordinary pin
        self.assertEqual(self.codes(drop("R3", "1"), src=self.SRC.replace("false", "true")), [])


if __name__ == "__main__":
    unittest.main()
