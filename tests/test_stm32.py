import json
import tempfile
import unittest
from pathlib import Path

from tests import _util
import stm32

CHIP = stm32.load_chip("stm32g474_lqfp64", Path("."))


def run(header, board=None, signals=(), fixed=None):
    d = Path(tempfile.mkdtemp())
    (d / "h.h").write_text(header)
    (d / "fw.json").write_text(json.dumps({"stm32": {
        "chip": "stm32g474_lqfp64", "header": "h.h", "signals": list(signals), "fixed": fixed or {},
        **({"pinmap": "b.json"} if board else {})}}))
    if board:
        (d / "b.json").write_text(json.dumps(board))
    import io, contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = stm32.cmd_pins(type("A", (), {"project": str(d), "json": False})())
    return rc, out.getvalue()


class Pins(unittest.TestCase):
    def test_clean(self):
        rc, out = run("#define PIN_A  A, 0\n#define ADC_A 1, 1\n",
                      {"signals": {"A": {"pin": "PA0", "function": "ADC1_IN1"}}})
        self.assertEqual(rc, 0, out)

    def test_wrong_adc_channel(self):
        rc, out = run("#define PIN_A A, 0\n#define ADC_A 1, 5\n")
        self.assertEqual(rc, 1)
        self.assertIn("does not carry ADC1_IN5", out)

    def test_duplicate_and_board_mismatch(self):
        rc, out = run("#define PIN_A A, 0\n#define PIN_B A, 0\n",
                      {"signals": {"A": "PA1"}})
        self.assertEqual(rc, 1)
        self.assertIn("used by both", out)
        self.assertIn("firmware PA0, board PA1", out)

    def test_fixed_and_missing(self):
        rc, out = run("#define PIN_USB_DM A, 5\n", signals=["X"], fixed={"USB_DM": "PA11"})
        self.assertEqual(rc, 1)
        self.assertIn("chip puts it on PA11", out)
        self.assertIn("X: not assigned", out)

    def test_no_pins_is_2(self):
        self.assertEqual(run("// nothing\n")[0], 2)


class Toolchain(unittest.TestCase):
    def test_pin_has_sha(self):
        self.assertIn(stm32.DEFAULT_TOOLCHAIN, stm32.TOOLCHAINS)
        self.assertEqual(len(stm32.TOOLCHAINS[stm32.DEFAULT_TOOLCHAIN]), 64)

    def test_env_prefix_wins(self):
        import os
        os.environ["ARM_PREFIX"] = "/x/arm-"
        try:
            self.assertEqual(stm32.find_prefix("14.2.1-1.1"), "/x/arm-")
        finally:
            del os.environ["ARM_PREFIX"]


if __name__ == "__main__":
    unittest.main()
