import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import _util  # noqa: F401
import check_all


class StageInto(unittest.TestCase):
    def test_target_pins_fill_in_and_src_is_kept_per_target(self):
        src, work, home = (Path(tempfile.mkdtemp()) for _ in range(3))
        (src / "fw.json").write_text(json.dumps({"host_tests": "mytests"}))
        (src / "build").mkdir()
        (src / "build" / "junk.o").write_text("x")
        (src / "main.c").write_text("int x;")
        with mock.patch.dict(os.environ, {"FW_HOME": str(home)}):
            proj = check_all.stage_into(src, work, {"host_tests": "test", "prepare": "sh fetch.sh {src}",
                                                    "idf": "v4.4.8"}, "lite")
        pins = json.loads((proj / "fw.json").read_text())
        self.assertEqual(pins["host_tests"], "mytests")  # the firmware's own pin wins
        self.assertEqual(pins["idf"], "v4.4.8")
        self.assertEqual(pins["src"], str(home / "check" / "lite" / "src"))
        self.assertFalse((proj / "build").exists())
        self.assertTrue((proj / "main.c").is_file())

    def test_examples_are_copied_as_they_are(self):
        src, work = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        (src / "fw.json").write_text("{}")
        proj = check_all.stage_into(src, work, None, "hello")
        self.assertEqual((proj / "fw.json").read_text(), "{}")


if __name__ == "__main__":
    unittest.main()
