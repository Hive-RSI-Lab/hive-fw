import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import _util  # noqa: F401
import fwidf


class Args(unittest.TestCase):
    def test_project_is_a_leading_dir(self):
        d = tempfile.mkdtemp()
        p, rest = fwidf.split_project([d, "-v"])
        self.assertEqual((p, rest), (Path(d).resolve(), ["-v"]))
        p, rest = fwidf.split_project(["-v"])
        self.assertEqual((p, rest), (Path.cwd(), ["-v"]))
        # A word that isn't a directory is an idf.py arg, not a project.
        p, rest = fwidf.split_project(["size-components"])
        self.assertEqual(rest, ["size-components"])

    def test_port_from_flag_or_env(self):
        with mock.patch.dict(os.environ, {"FW_PORT": "rfc2217://bench:4000"}):
            self.assertEqual(fwidf.port_arg(["-b", "921600"]), ("rfc2217://bench:4000", ["-b", "921600"]))
            self.assertEqual(fwidf.port_arg(["--port", "/dev/ttyUSB1"]), ("/dev/ttyUSB1", []))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(fwidf.port_arg([]), (None, []))

    def test_flash_without_a_port_says_how(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(fwidf.cmd_flash([tempfile.mkdtemp()]), 2)


class Overlay(unittest.TestCase):
    def test_overlay_replaces_set_and_unset_options(self):
        base = "CONFIG_A=y\n# CONFIG_B is not set\nCONFIG_C=1\n# a comment\n"
        out = fwidf.overlay_sdkconfig(base, ["# CONFIG_A is not set", "CONFIG_B=y"]).splitlines()
        self.assertNotIn("CONFIG_A=y", out)
        self.assertNotIn("# CONFIG_B is not set", out)
        self.assertEqual(out[-2:], ["# CONFIG_A is not set", "CONFIG_B=y"])
        self.assertIn("CONFIG_C=1", out)
        self.assertIn("# a comment", out)

    def test_overlay_drops_deprecated_aliases(self):
        base = "CONFIG_ESPTOOLPY_FLASHMODE_QIO=y\nCONFIG_FLASHMODE_QIO=y\nCONFIG_OTHER=y\n"
        renames = {"CONFIG_FLASHMODE_QIO": "CONFIG_ESPTOOLPY_FLASHMODE_QIO"}
        out = fwidf.overlay_sdkconfig(base, ["# CONFIG_ESPTOOLPY_FLASHMODE_QIO is not set"], renames)
        self.assertEqual(out.splitlines(), ["CONFIG_OTHER=y", "# CONFIG_ESPTOOLPY_FLASHMODE_QIO is not set"])
        # and the other way: an overlay naming the old name drops the new one
        out = fwidf.overlay_sdkconfig(base, ["CONFIG_FLASHMODE_QIO=n"], renames)
        self.assertEqual(out.splitlines(), ["CONFIG_OTHER=y", "CONFIG_FLASHMODE_QIO=n"])

    def test_renames_read_from_idf_and_project(self):
        d = Path(tempfile.mkdtemp())
        (d / "components" / "c").mkdir(parents=True)
        (d / "components" / "c" / "sdkconfig.rename").write_text(
            "# old new\nCONFIG_OLD_A   CONFIG_NEW_A\nCONFIG_OLD_B !CONFIG_NEW_B\n\n")
        (d / "sdkconfig.rename").write_text("CONFIG_OLD_C CONFIG_NEW_C\n")
        self.assertEqual(fwidf.sdkconfig_renames(d, d / "missing"),
                         {"CONFIG_OLD_A": "CONFIG_NEW_A", "CONFIG_OLD_B": "CONFIG_NEW_B",
                          "CONFIG_OLD_C": "CONFIG_NEW_C"})


if __name__ == "__main__":
    unittest.main()
