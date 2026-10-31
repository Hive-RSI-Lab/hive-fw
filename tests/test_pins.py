import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import _util  # noqa: F401
import pins

# U5's pins as hwde's KiCad 10 netlist names them (EasyEDA's pin-number suffix).
U5 = {"25": ("IO0_25", "/I2S_MCLK"), "26": ("IO4_26", "/PD_PG"), "33": ("IO21_33", "/AMP_SDZ_IO"),
      "37": ("IO23_37", "/AMP_FAULTZ"), "8": ("IO32_8", "/I2C_SCL"), "9": ("IO33_9", "/I2C_SDA"),
      "12": ("IO27_12", "/I2S_BCLK"), "4": ("SENSOR_VP_4", "/BTN_VOLDN"), "16": ("IO13_16", "/BTN_VOLUP"),
      "5": ("SENSOR_VN_5", "/PVDD_SENSE"), "6": ("IO34_6", "/VBUS_SENSE"), "13": ("IO14_13", "/IR_IN"),
      "24": ("IO2_24", "unconnected-(U5-IO2-Pad24)"), "2": ("3V3_2", "+3V3"), "1": ("GND_1", "GND")}


def netlist(u5=U5) -> str:
    nets = "".join(f'(net (code "{i}") (name "{net}") (node (ref "U5") (pin "{pin}") (pinfunction "{fn}")))'
                   for i, (pin, (fn, net)) in enumerate(u5.items(), 1))
    return ('(export (version "E") (components (comp (ref "U5") (value "ESP32-WROVER-E-N8R8") '
            '(footprint "aiee:WIFI-SMD_ESP32-WROVER-IE8MB")) (comp (ref "R1") (value "10k"))) '
            f"(nets {nets}))")


HEADER = """#define PIN_PD_PG 4      /* low = contract good */
#define PIN_AMP_SDZ 21
#define PIN_AMP_FAULTZ 23
#define ADC_VBUS ADC1_CHANNEL_6 /* GPIO34 */
#define I2C_PORT 0
#define I2C_SDA 33
#define I2C_SCL 32
#define ES8388_ADDR 0x10
"""
PATCH = r"""--- a/Kconfig
+++ b/Kconfig
 			default "model=TAS57xx,bck=33,ws=25,do=32,sda=27,scl=26,mute=14:0" if SQUEEZEAMP
+			default "model=ES8388,bck=27,sda=33,scl=32,i2c=16,mck=0" if HIFI_LITE
+			default "[{\"gpio\":13,\"normal\":{\"pressed\":\"ACTRLS_VOLUP\"}},{\"gpio\":36,\"normal\":{\"pressed\":\"ACTRLS_VOLDOWN\"}}]" if HIFI_LITE
+			default "14=ir:nec" if HIFI_LITE
"""


class Fixture(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.fw = self.d / "firmware"
        (self.fw / "main").mkdir(parents=True)
        (self.fw / "main" / "board.h").write_text(HEADER)
        (self.fw / "hooks.patch").write_text(PATCH)
        (self.d / "board.net").write_text(netlist())
        self.cfg = {"board": "../board.net", "sources": ["main/*.h", "*.patch"]}

    def run_check(self):
        return pins.check(self.fw, self.cfg)

    def rows(self, res):
        return {(r["gpio"], r["name"]): r for r in res["boards"][0]["pins"]}


class Firmware(Fixture):
    def test_reads_defines_adc_and_squeezelite_strings_from_added_lines(self):
        got = {(p["gpio"], p["name"]) for p in pins.firmware_pins(self.fw, self.cfg["sources"])}
        self.assertEqual(got, {(4, "PIN_PD_PG"), (21, "PIN_AMP_SDZ"), (23, "PIN_AMP_FAULTZ"),
                               (34, "ADC_VBUS"), (33, "I2C_SDA"), (32, "I2C_SCL"),
                               (27, "bck"), (33, "sda"), (32, "scl"), (0, "mck"),
                               (13, "ACTRLS_VOLUP"), (36, "ACTRLS_VOLDOWN"), (14, "ir")})
        # not I2C_PORT, ES8388_ADDR, i2c=16 (an address), or SQUEEZEAMP's context line

    def test_an_i2c_devices_bits_are_not_gpios(self):
        (self.fw / "front.h").write_text("#define FRONT_ADDR 0x27\n#define FRONT_LED_R 9 /* P11 */\n#define FRONT_LED_PIN 10\n")
        got = {p["name"] for p in pins.firmware_pins(self.fw, ["front.h"])}
        self.assertEqual(got, set())

    def test_a_bare_clk_is_an_spi_clock_not_the_ethernet_one(self):
        self.assertGreater(pins.match("clk", "LCD_SCLK"), pins.match("clk", "ETH_CLK"))

    def test_when_keeps_one_boards_kconfig_defaults(self):
        (self.fw / "hooks.patch").write_text(PATCH + '+\t\t\tdefault "model=I2S,bck=2,ws=4" if HIFI_PLUS || HIFI_ULTRA\n')
        names = lambda when: {(p["gpio"], p["name"]) for p in pins.firmware_pins(self.fw, ["*.patch"], when)}
        self.assertIn((2, "bck"), names(None))
        self.assertNotIn((2, "bck"), names("HIFI_LITE"))
        self.assertIn((27, "bck"), names("HIFI_LITE"))
        self.assertIn((2, "bck"), names("HIFI_PLUS"))

    def test_without_when_the_boards_own_sdkconfig_picks_its_lines(self):
        # the hifi receiver's case: one Kconfig patch with Lite, Plus and Ultra
        # defaults, fw.json naming only the Lite board, board/<name>/sdkconfig.defaults
        (self.fw / "hooks.patch").write_text(PATCH + '+\t\t\tdefault "model=I2S,bck=2,ws=4" if HIFI_PLUS || HIFI_ULTRA\n'
                                             '+\t\t\tdefault "sda=13,scl=14" if !HIFI_LITE\n')
        names = lambda cfg: {(p["gpio"], p["name"]) for p in
                             pins.firmware_pins(self.fw, ["*.patch"], pins.resolve_when(self.fw, cfg))}
        cfg = {"board": "../boards/lite-main/kicad/lite-main.kicad_pcb", "sources": ["*.patch"]}
        self.assertIn((2, "bck"), names(cfg))  # no sdkconfig: every line counts
        for b, on in (("lite-main", "HIFI_LITE"), ("core", "HIFI_PLUS")):
            (self.fw / "board" / b).mkdir(parents=True)
            (self.fw / "board" / b / "sdkconfig.defaults").write_text(
                f"CONFIG_{on}=y\n# CONFIG_{'HIFI_PLUS' if on == 'HIFI_LITE' else 'HIFI_LITE'} is not set\n")
        self.assertEqual(pins.board_sdkconfig(self.fw, cfg), self.fw / "board/lite-main/sdkconfig.defaults")
        got = names(cfg)
        self.assertNotIn((2, "bck"), got)
        self.assertNotIn((13, "sda"), got)
        self.assertIn((27, "bck"), got)
        got = names({**cfg, "board": "../boards/core/kicad/core.net"})
        self.assertIn((2, "bck"), got)
        self.assertIn((13, "sda"), got)
        self.assertNotIn((27, "bck"), got)
        self.assertIn((27, "bck"), names({**cfg, "when": "HIFI_LITE"}))  # a named when still wins
        self.assertFalse(pins.cond_holds("A && !B", {"A": True, "B": True}))
        self.assertTrue(pins.cond_holds("(A || B) && !C", {"B": True}))
        self.assertTrue(pins.cond_holds("FOO = y", {}))  # unread: counts


class Board(Fixture):
    def test_gpio_names(self):
        for fn, g in (("IO23_37", 23), ("GPIO5", 5), ("SENSOR_VN_5", 39), ("IO0/BOOT", 0),
                      ("U0TXD", 1), ("RXD0_34", 3), ("3V3_2", None), ("NC_17", None)):
            self.assertEqual(pins.gpio_of(fn), g, fn)

    def test_netlist(self):
        (b,) = pins.read_board(self.d / "board.net")[0]
        self.assertEqual((b.ref, b.psram), ("U5", True))
        self.assertEqual(b.gpios[0], "I2S_MCLK")
        self.assertIsNone(b.gpios[2])

    def test_layout_without_pin_names_uses_the_module_pads(self):
        pcb = ('(kicad_pcb (footprint "aiee:WIFI-SMD_ESP32-WROVER-IE8MB" (property "Reference" "U5") '
               '(property "Value" "ESP32-WROVER-E") (pad "25" smd rect (net "/I2S_MCLK")) '
               '(pad "24" smd rect) (pad "1" smd rect (net "GND"))) '
               '(footprint "R" (property "Reference" "R1") (pad "1" smd (net "/I2S_MCLK"))))')
        (self.d / "b.kicad_pcb").write_text(pcb)
        (b,) = pins.read_board(self.d / "b.kicad_pcb")[0]
        self.assertEqual(b.gpios, {0: "I2S_MCLK", 2: None})

    def test_schematic_without_kicad_cli_reads_the_netlist_beside_it(self):
        (self.d / "board.kicad_sch").write_text("(kicad_sch)")
        with mock.patch.object(pins, "kicad_cli", return_value=None):
            (b,), note = pins.read_board(self.d / "board.kicad_sch")
        self.assertIn("may be older", note)
        self.assertEqual(b.ref, "U5")


class Check(Fixture):
    def test_the_lite_map_passes(self):
        res = self.run_check()
        self.assertTrue(res["ok"], json.dumps(res, indent=1))
        self.assertEqual(res["boards"][0]["unused"], {39: "PVDD_SENSE"})

    def test_swapped_i2c_fails_and_says_where_the_net_is(self):
        (self.fw / "main" / "board.h").write_text(HEADER.replace("SDA 33", "SDA 32").replace("SCL 32", "SCL 33"))
        res = self.run_check()
        self.assertFalse(res["ok"])
        r = self.rows(res)[(32, "I2C_SDA")]
        self.assertEqual(r["verdict"], "fail")
        self.assertIn("I2C_SDA on GPIO33", r["why"])
        self.assertEqual(self.rows(res)[(33, "sda")]["verdict"], "pass")

    def test_amp_pins_swapped_fail_though_both_say_amp(self):
        (self.fw / "main" / "board.h").write_text(HEADER.replace("SDZ 21", "SDZ 23").replace("FAULTZ 23", "FAULTZ 21"))
        res = self.run_check()
        self.assertEqual(self.rows(res)[(23, "PIN_AMP_SDZ")]["verdict"], "fail")
        self.assertEqual(self.rows(res)[(21, "PIN_AMP_FAULTZ")]["verdict"], "fail")

    def test_unconnected_psram_and_unmatched(self):
        (self.fw / "main" / "board.h").write_text(
            HEADER + "#define PIN_SPARE 2\n#define PIN_CS 16\n#define PIN_MYSTERY 13\n")
        rows = self.rows(self.run_check())
        self.assertIn("isn't connected", rows[(2, "PIN_SPARE")]["why"])
        self.assertIn("PSRAM", rows[(16, "PIN_CS")]["why"])
        self.assertIn("no net", rows[(13, "PIN_MYSTERY")]["why"])

    def test_nets_name_a_pins_net_and_ignore_drops_one(self):
        (self.fw / "main" / "board.h").write_text(HEADER + "#define PIN_MYSTERY 13\n#define PIN_SPARE 2\n")
        self.cfg.update(nets={"PIN_MYSTERY": "BTN_VOLUP"}, ignore=["PIN_SPARE"])
        self.assertTrue(self.run_check()["ok"])
        self.cfg["nets"] = {"PIN_MYSTERY": "/IR_IN"}
        r = self.rows(self.run_check())[(13, "PIN_MYSTERY")]
        self.assertEqual((r["verdict"], r["why"]), ("fail", "IR_IN is on GPIO14"))

    def test_cant_run_without_a_board_or_an_esp32(self):
        self.assertIn("no board", pins.check(self.fw, {"sources": ["*.patch"]})["error"])
        (self.d / "board.net").write_text('(export (components (comp (ref "R1"))) (nets))')
        self.assertIn("no ESP32", self.run_check()["error"])

    def test_cli_exit_codes(self):
        (self.fw / "fw.json").write_text(json.dumps({"pinmap": self.cfg}))
        with mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(pins.main([str(self.fw)]), 0)
        self.assertIn("pins: pass", out.getvalue())
        (self.fw / "main" / "board.h").write_text(HEADER.replace("PD_PG 4", "PD_PG 13"))
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(pins.main([str(self.fw), "--json"]), 1)
            self.assertEqual(pins.main([str(self.fw), "--board", str(self.d / "nope.net")]), 2)


if __name__ == "__main__":
    unittest.main()
