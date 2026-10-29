import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import _util  # noqa: F401
import fwlib


class Pins(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_defaults(self):
        p = fwlib.read_pins(self.d)
        self.assertEqual(p["idf"], fwlib.DEFAULT_IDF)
        self.assertEqual(p["target"], "esp32")

    def test_fw_json_wins(self):
        (self.d / "fw.json").write_text(json.dumps({"idf": "v5.3.6", "target": "esp32s3"}))
        (self.d / ".idf-version").write_text("v4.4.8\n")
        p = fwlib.read_pins(self.d)
        self.assertEqual((p["idf"], p["target"]), ("v5.3.6", "esp32s3"))

    def test_squeezelite_dockerfile(self):
        # squeezelite-esp32 pins its IDF in the Dockerfile it builds in.
        (self.d / "Dockerfile").write_text("FROM ubuntu:20.04\nARG IDF_CLONE_BRANCH_OR_TAG=v4.4.8\n")
        self.assertEqual(fwlib.read_pins(self.d)["idf"], "v4.4.8")

    def test_bad_version_refused(self):
        for bad in ("master", "v4.4; rm -rf /", "../x", ""):
            with self.assertRaises(ValueError):
                fwlib.check_version(bad)
        (self.d / "fw.json").write_text(json.dumps({"idf": "release/v5.1"}))
        with self.assertRaises(ValueError):
            fwlib.read_pins(self.d)

    def test_env_pin(self):
        (self.d / "fw.json").write_text(json.dumps({"env": {"HIFI_BOARD": "core"}}))
        self.assertEqual(fwlib.read_pins(self.d)["env"], {"HIFI_BOARD": "core"})
        for bad in (["HIFI_BOARD=core"], {"HIFI_BOARD": 1}, {"A B": "x"}):
            (self.d / "fw.json").write_text(json.dumps({"env": bad}))
            with self.assertRaises(ValueError):
                fwlib.read_pins(self.d)

    def test_python_for(self):
        self.assertEqual(fwlib.python_for("v4.4.8"), "3.10")
        self.assertEqual(fwlib.python_for("v5.5.6"), "3.12")
        self.assertEqual(fwlib.python_for("v6.1-rc1"), "3.12")


class Roots(unittest.TestCase):
    def test_shared_before_home(self):
        shared, home = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        for r in (shared / "fw", home):
            (r / "esp-idf-v4.4.8").mkdir(parents=True)
            (r / "esp-idf-v4.4.8" / ".fw-ok").write_text("/x\n")
        with mock.patch.dict(os.environ, {"HIVE_TOOLS": str(shared), "FW_HOME": str(home)}):
            os.environ.pop("FW_TOOLS", None)
            self.assertEqual(fwlib.find_idf("v4.4.8"), shared / "fw")
            self.assertIsNone(fwlib.find_idf("v5.5.6"))
            # A half-done install (no .fw-ok) doesn't count.
            (home / "esp-idf-v5.5.6").mkdir()
            self.assertIsNone(fwlib.find_idf("v5.5.6"))


if __name__ == "__main__":
    unittest.main()
