import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import _util
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



class Proofs(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.ex = self.root / "examples" / "a"
        self.ex.mkdir(parents=True)
        (self.ex / "fw.json").write_text("{}")
        (self.ex / "main.c").write_text("int x;")
        (self.root / "proof").mkdir()

    def test_stale_proof_counts_nothing(self):
        (self.root / "proof" / "a.json").write_text(json.dumps(
            {"inputs": check_all.inputs_hash(self.ex), "stages": {"build": "pass", "qemu": "fail"}}))
        self.assertEqual(check_all.proven(self.root, "a", self.ex), (["build"], ""))
        (self.ex / "main.c").write_text("int y;")
        self.assertIn("stale", check_all.proven(self.root, "a", self.ex)[1])

    def test_build_output_doesnt_change_the_hash(self):
        h = check_all.inputs_hash(self.ex)
        (self.ex / "build").mkdir()
        (self.ex / "build" / "x.o").write_text("junk")
        (self.ex / "sdkconfig").write_text("CONFIG_X=y")
        self.assertEqual(check_all.inputs_hash(self.ex), h)

    def test_every_example_has_a_fresh_proof(self):
        """The module's own record holds: every example has a proof made on its
        current files. Edit an example, run `fw check-all NAME`, commit proof/."""
        for n in check_all.examples():
            _, why = check_all.proven(_util.ROOT, n, _util.ROOT / "examples" / n)
            self.assertEqual(why, "", f"examples/{n}: {why}")


class MainFiles(unittest.TestCase):
    def test_a_target_is_read_from_its_main_not_its_working_tree(self):
        projects = Path(tempfile.mkdtemp())
        fw = projects / "rx" / "firmware"
        fw.mkdir(parents=True)
        (fw / "main.c").write_text("int x;")

        def git(*a):
            subprocess.run(["git", "-C", str(projects / "rx"), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                           check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        git("add", "-A")
        git("commit", "-qm", "x")
        (fw / "main.c").write_text("int dirty;")
        with mock.patch.dict(os.environ, {"HIVE_PROJECTS": str(projects)}):
            d = check_all.main_files({"project": "rx", "path": "firmware"}, Path(tempfile.mkdtemp()))
            self.assertEqual((d / "main.c").read_text(), "int x;")
            self.assertIn("not on this machine",
                          check_all.main_files({"project": "nope"}, Path(tempfile.mkdtemp())))


if __name__ == "__main__":
    unittest.main()
