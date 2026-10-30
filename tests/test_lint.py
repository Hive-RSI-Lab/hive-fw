import tempfile
import unittest
from pathlib import Path

from tests import _util  # noqa: F401
import lint

NET = """(export (components (comp (ref "U1") (value "ESP32-WROVER-E")))
 (nets
  (net (code "1") (name "/AMP_EN") (node (ref "U1") (pin "33") (pinfunction "IO21_33")))
  (net (code "2") (name "/BTN_VOLDN") (node (ref "U1") (pin "4") (pinfunction "SENSOR_VP_4")))
  (net (code "3") (name "unconnected-(U1-IO5-Pad29)") (node (ref "U1") (pin "29") (pinfunction "IO5_29")))
  (net (code "4") (name "/I2S_BCLK") (node (ref "U1") (pin "12") (pinfunction "IO27_12")))
 ))
"""


def codes(found):
    return sorted({f.code for f in found if f.error})


class Pins(unittest.TestCase):
    def test_netlist_pads_map_to_gpios(self):
        self.assertEqual(lint.module_nets(NET, "U1"),
                         {21: "/AMP_EN", 36: "/BTN_VOLDN", 5: "unconnected-(U1-IO5-Pad29)", 27: "/I2S_BCLK"})

    def test_header_and_kconfig_pins(self):
        ps = lint.header_pins("#define PIN_AMP_EN 21 /* x */\n#define ES_ADDR 0x10\n"
                              "#define ADC_VBUS ADC1_CHANNEL_6\n", {"PIN_AMP_EN"})
        self.assertEqual([(p.gpio, p.out, p.adc) for p in ps], [(21, True, 0), (34, False, 1)])
        k = ('+   default "model=X,bck=27,ws=-1,i2c=16" if BOARD_A\n'
             '    default "bck=5" if BOARD_B\n'
             '+   default "[{\\"gpio\\":36,\\"normal\\":{\\"pressed\\":\\"ACTRLS_VOLDOWN\\"}}]" if BOARD_A || C\n'
             '+   default "14=ir:nec,22=green:1" if BOARD_A\n')
        got = sorted((p.gpio, sorted(p.words), p.out) for p in lint.kconfig_pins(k, "BOARD_A"))
        self.assertEqual(got, [(14, ["IR"], False), (22, ["LED"], True), (27, ["BCLK"], True),
                               (36, ["VOLDN"], False)])

    def check(self, *pins, cfg=None):
        return lint.check_pins(list(pins), lint.module_nets(NET, "U1"), cfg or {}, "ESP32-WROVER-E")

    def test_clean_pins_pass_and_unused_nets_are_notes(self):
        f = self.check(lint.Pin(21, "PIN_AMP_EN", lint.words("PIN_AMP_EN"), True),
                       lint.Pin(27, "bck=27", frozenset({"BCLK"}), True))
        self.assertEqual(codes(f), [])
        self.assertIn("GPIO36", " ".join(str(x) for x in f if not x.error))

    def test_each_rule(self):
        P = lint.Pin
        self.assertEqual(codes(self.check(P(27, "PIN_AMP_EN", lint.words("PIN_AMP_EN")))), ["PIN-NET"])
        self.assertEqual(codes(self.check(P(5, "PIN_X", frozenset({"X"})))), ["PIN-NET"])
        self.assertIn("PIN-FLASH", codes(self.check(P(7, "PIN_X", frozenset({"X"})))))
        self.assertIn("PIN-FLASH", codes(self.check(P(16, "PIN_X", frozenset({"X"})))))
        self.assertEqual(codes(self.check(P(36, "PIN_VOLDN_EN", lint.words("PIN_VOLDN_EN"), True))), ["PIN-INPUT"])
        self.assertIn("STRAP-12", codes(self.check(P(12, "PIN_X", frozenset({"X"})))))
        adc = P(27, "ADC_BCLK", frozenset({"BCLK"}), False, 2)
        self.assertEqual(codes(self.check(adc, cfg={"CONFIG_ESP32_WIFI_STATIC_RX_BUFFER_NUM": "10"})), ["ADC2-WIFI"])
        self.assertEqual(codes(self.check(adc)), [])  # no Wi-Fi
        self.assertIn("PIN-SHARED", codes(self.check(P(21, "PIN_AMP_EN", lint.words("PIN_AMP_EN")),
                                                     P(21, "button PLAY", frozenset({"PLAY"})))))


class Partitions(unittest.TestCase):
    def check(self, csv, cfg=None, sizes=None):
        return codes(lint.check_partitions(csv, cfg or {"CONFIG_ESPTOOLPY_FLASHSIZE": "4MB"}, sizes or {}))

    T = "# n, t, s, o, size\nnvs, data, nvs, 0x9000, 0x4000,\notadata, data, ota, , 0x2000,\n" \
        "ota_0, app, ota_0, 0x10000, 1M,\nota_1, app, ota_1, , 1M,\n"

    def test_a_good_table(self):
        self.assertEqual(self.check(self.T), [])
        self.assertEqual(self.check(self.T, sizes={"ota_0": 900000}), [])

    def test_each_rule(self):
        self.assertEqual(self.check(self.T, {"CONFIG_ESPTOOLPY_FLASHSIZE": "2MB"}), ["PART-FLASH"])
        self.assertEqual(self.check(self.T, sizes={"ota_0": 0x100001}), ["PART-IMAGE"])
        self.assertEqual(self.check(self.T.replace("0x9000", "0x8000")), ["PART-OVERLAP"])
        self.assertIn("PART-ALIGN", self.check(self.T.replace("0x10000", "0x11000")))
        self.assertEqual(self.check(self.T.replace("otadata, data, ota, , 0x2000,\n", "")), ["PART-OTA"])
        self.assertIn("PART-OVERLAP", self.check(self.T.replace("0x4000", "0x8000")))


class Cli(unittest.TestCase):
    def test_exit_codes(self):
        d = Path(tempfile.mkdtemp())
        (d / "lint.json").write_text('{"partitions": "p.csv"}')
        (d / "p.csv").write_text("factory, app, factory, 0x10000, 1M,\n")
        self.assertEqual(lint.main([str(d)]), 0)
        (d / "p.csv").write_text("factory, app, factory, 0x10800, 1M,\n")
        self.assertEqual(lint.main([str(d)]), 1)
        self.assertEqual(lint.main([str(d / "nope")]), 2)


if __name__ == "__main__":
    unittest.main()
