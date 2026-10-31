import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import _util  # noqa: F401
import dts

# U1, an i.MX6ULL as hw-lab's linux-computer draws it: pin names are the pads
# (EasyEDA's ball suffix on some), numbers the balls.
U1 = {"F15": ("ENET1_TX_EN", "/ETH_TXEN"), "E14": ("ENET1_TX_DATA0", "/ETH_TXD0"),
      "E15": ("ENET1_TX_DATA1", "/ETH_TXD1"), "F16": ("ENET1_RX_DATA0", "/ETH_RXD0"),
      "F14": ("ENET1_RX_DATA1", "/ETH_RXD1"), "G13": ("ENET1_RX_EN", "/ETH_CRS_DV"),
      "F13": ("ENET1_TX_CLK", "/ETH_REFCLK"), "K17": ("GPIO1_IO06_K17", "/ETH_MDIO"),
      "L16": ("GPIO1_IO07", "/ETH_MDC"), "K14": ("UART1_TX_DATA", "/DBG_TX"),
      "K16": ("UART1_RX_DATA", "/DBG_RX"), "M17": ("GPIO1_IO03", "/LED_STATUS"),
      "N17": ("GPIO1_IO09", "unconnected-(U1-GPIO1_IO09-PadN17)"), "M15": ("GPIO1_IO04", "/SPARE4"),
      "A1": ("VSS_A1", "GND")}


def netlist(u1=U1) -> str:
    nets = "".join(f'(net (code "{i}") (name "{net}") (node (ref "U1") (pin "{pin}") (pinfunction "{fn}")))'
                   for i, (pin, (fn, net)) in enumerate(u1.items(), 1))
    return ('(export (version "E") (components (comp (ref "U1") (value "MCIMX6Y2CVM08AB")) '
            '(comp (ref "U2") (value "LAN8720A"))) '
            f'(nets {nets} (net (code "99") (name "/ETH_TXEN") (node (ref "U2") (pin "1") (pinfunction "TXEN")))))')


DTSI = """/* the SoC part */
&iomuxc {
	pinctrl_enet1: enet1grp {
		fsl,pins = <
			MX6UL_PAD_GPIO1_IO07__ENET1_MDC		0x1b0b0
			MX6UL_PAD_GPIO1_IO06__ENET1_MDIO	0x1b0b0
			MX6UL_PAD_ENET1_RX_EN__ENET1_RX_EN	0x1b0b0
			MX6UL_PAD_ENET1_RX_DATA0__ENET1_RDATA00	0x1b0b0
			MX6UL_PAD_ENET1_RX_DATA1__ENET1_RDATA01	0x1b0b0
			MX6UL_PAD_ENET1_TX_EN__ENET1_TX_EN	0x1b0b0
			MX6UL_PAD_ENET1_TX_DATA0__ENET1_TDATA00	0x1b0b0
			MX6UL_PAD_ENET1_TX_DATA1__ENET1_TDATA01	0x1b0b0
			MX6UL_PAD_ENET1_TX_CLK__ENET1_REF_CLK1	0x4001b031
		>;
	};
};
"""
DTS = """// SPDX-License-Identifier: GPL-2.0
/dts-v1/;
#include "imx6ull.dtsi"
#include "lc-iomuxc.dtsi"
#include <dt-bindings/gpio/gpio.h>

/ {
	model = "linux-computer";
};

&iomuxc {
	pinctrl_uart1: uart1grp {
		fsl,pins = <
			MX6UL_PAD_UART1_TX_DATA__UART1_DCE_TX 0x1b0b1
			MX6UL_PAD_UART1_RX_DATA__UART1_DCE_RX 0x1b0b1
		>;
	};
	pinctrl_led: ledgrp {
		fsl,pins = <MX6UL_PAD_GPIO1_IO03__GPIO1_IO03 0x10b0>;
	};
};
"""


class Fixture(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        (self.d / "linux").mkdir()
        (self.d / "linux" / "lc.dts").write_text(DTS)
        (self.d / "linux" / "lc-iomuxc.dtsi").write_text(DTSI)
        (self.d / "lc.net").write_text(netlist())
        self.cfg = {"files": ["linux/lc.dts"], "board": "lc.net"}

    def run_check(self, cfg=None):
        return dts.check(self.d, cfg or self.cfg)

    def rows(self, res):
        return {r["pad"]: r for r in res["boards"][0]["pins"]}


class Reading(Fixture):
    def test_entries_follow_includes_and_skip_comments(self):
        es = dts.entries(self.d / "linux" / "lc.dts")
        self.assertEqual(len(es), 12)
        e = next(x for x in es if x["pad"] == "ENET1_TX_CLK")
        self.assertEqual((e["function"], e["family"], e["where"]), ("ENET1_REF_CLK1", "imx", "lc-iomuxc.dtsi:13"))
        self.assertIn("pinctrl_led:ledgrp", next(x for x in es if x["pad"] == "GPIO1_IO03")["group"])

    def test_other_families(self):
        p = self.d / "o.dts"
        p.write_text("""/ { soc {
	usart1_pins_a: usart1-0 { pins1 { pinmux = <STM32_PINMUX('A', 9, AF7)>, <STM32_PINMUX('A', 10, AF7)>; }; };
	uart0_pb_pins: uart0-pb-pins { pins = "PB8", "PB9"; function = "uart0"; };
	i2c1_xfer: i2c1-xfer { rockchip,pins = <1 RK_PB2 1 &pcfg_pull_none>, <1 RK_PB3 1 &pcfg_pull_none>; };
	imx8: enetgrp { fsl,pins = <MX8MM_IOMUXC_ENET_MDC_ENET1_MDC 0x3>; };
}; };
""")
        got = {(e["pad"], e["function"], e["family"]) for e in dts.entries(p)}
        self.assertEqual(got, {("PA9", "AF7", "stm32"), ("PA10", "AF7", "stm32"), ("PB8", "uart0", "sunxi"),
                               ("PB9", "uart0", "sunxi"), ("GPIO1_B2", "", "rockchip"),
                               ("GPIO1_B3", "", "rockchip"), (None, "", "imx")})

    def test_tokens_meet_on_the_signal(self):
        self.assertGreater(dts.score("ENET1_TDATA00", "ETH_TXD0"), dts.score("ENET1_TDATA00", "ETH_TXD1"))
        self.assertGreater(dts.score("UART1_DCE_TX", "DBG_TX"), dts.score("UART1_DCE_TX", "DBG_RX"))
        self.assertGreater(dts.score("ENET1_REF_CLK1", "ETH_REFCLK"), dts.score("ENET1_REF_CLK1", "ETH_MDC"))
        self.assertGreater(dts.score("ENET1_RX_EN", "ETH_CRS_DV"), dts.score("ENET1_RX_EN", "ETH_RXD1"))


class Check(Fixture):
    def test_the_board_as_drawn_passes(self):
        res = self.run_check()
        self.assertEqual(res["error"], "")
        fails = [r for r in res["boards"][0]["pins"] if r["verdict"] == "fail"]
        self.assertEqual(fails, [])
        self.assertTrue(res["ok"])
        b = res["boards"][0]
        self.assertEqual((b["ref"], b["part"]), ("U1", "MCIMX6Y2CVM08AB"))
        self.assertEqual(self.rows(res)["GPIO1_IO03"]["verdict"], "pass")   # LED_STATUS meets ledgrp

    def test_a_signal_moved_in_the_schematic_and_not_the_dts_fails(self):
        u1 = dict(U1)
        # the PHY's TXD0 moved to a spare pad; the DTS still muxes ENET1_TX_DATA0
        u1["E14"] = ("ENET1_TX_DATA0", "unconnected-(U1-ENET1_TX_DATA0-PadE14)")
        u1["M15"] = ("GPIO1_IO04", "/ETH_TXD0")
        (self.d / "lc.net").write_text(netlist(u1))
        r = self.rows(self.run_check())
        self.assertEqual((r["ENET1_TX_DATA0"]["verdict"], r["ENET1_TX_DATA0"]["code"]), ("fail", "PAD-NC"))
        u1["E14"] = ("ENET1_TX_DATA0", "/LED2")
        (self.d / "lc.net").write_text(netlist(u1))
        r = self.rows(self.run_check())["ENET1_TX_DATA0"]
        self.assertEqual((r["verdict"], r["code"]), ("fail", "PAD-NET"))
        self.assertIn("ETH_TXD0 on GPIO1_IO04", r["why"])

    def test_swapped_pair_fails_both(self):
        u1 = dict(U1)
        u1["E14"], u1["E15"] = ("ENET1_TX_DATA0", "/ETH_TXD1"), ("ENET1_TX_DATA1", "/ETH_TXD0")
        (self.d / "lc.net").write_text(netlist(u1))
        res = self.run_check()
        self.assertFalse(res["ok"])
        r = self.rows(res)
        self.assertEqual([r[p]["code"] for p in ("ENET1_TX_DATA0", "ENET1_TX_DATA1")], ["PAD-NET", "PAD-NET"])
        self.assertEqual(r["ENET1_TX_EN"]["verdict"], "pass")

    def test_typo_pad_and_unconnected_gpio(self):
        (self.d / "linux" / "lc.dts").write_text(DTS.replace("GPIO1_IO03__GPIO1_IO03", "GPIO1_IO09__GPIO1_IO09")
                                                 .replace("UART1_RX_DATA__", "UART1_RXD_DATA__"))
        r = self.rows(self.run_check())
        self.assertEqual(r["UART1_RXD_DATA"]["code"], "PAD-NONE")
        self.assertEqual(r["GPIO1_IO09"]["code"], "PAD-NC")

    def test_nets_and_ignore(self):
        cfg = dict(self.cfg, nets={"UART1_TX_DATA": "CONSOLE_TX"}, ignore=["gpio1_io03"])
        r = self.rows(self.run_check(cfg))
        self.assertEqual((r["UART1_TX_DATA"]["code"], r["GPIO1_IO03"]["verdict"]), ("PAD-NET", "note"))

    def test_cant_run(self):
        self.assertIn("dts.files", dts.check(self.d, {"board": "lc.net"})["error"])
        self.assertIn("dts.board", dts.check(self.d, {"files": ["linux/lc.dts"]})["error"])
        (self.d / "empty.net").write_text('(export (components) (nets))')
        self.assertIn("no part", dts.check(self.d, {"files": ["linux/lc.dts"], "board": "empty.net"})["error"])
        self.assertIn("no U9", self.run_check(dict(self.cfg, ref="U9"))["error"])

    def test_cli(self):
        (self.d / "fw.json").write_text(json.dumps({"dts": self.cfg}))
        with mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(dts.main([str(self.d)]), 0)
        self.assertIn("dts: pass", out.getvalue())
        (self.d / "linux" / "lc.dts").write_text(DTS.replace("UART1_RX_DATA__", "UART1_RXD_DATA__"))
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(dts.main([str(self.d), "--json"]), 1)
            self.assertEqual(dts.main([str(self.d), "--board", str(self.d / "nope.net")]), 2)


if __name__ == "__main__":
    unittest.main()
