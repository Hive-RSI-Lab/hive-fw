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


if __name__ == "__main__":
    unittest.main()
