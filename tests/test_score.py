import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import _util
import score


class Score(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        ex = self.root / "examples" / "a"
        ex.mkdir(parents=True)
        (ex / "fw.json").write_text("{}")
        (ex / "main.c").write_text("int x;")
        (self.root / "proof").mkdir()

    def prove(self, **stages):
        rec = {"inputs": score.inputs_hash(self.root / "examples" / "a"), "stages": stages}
        (self.root / "proof" / "a.json").write_text(json.dumps(rec))

    def test_no_proof_is_zero(self):
        self.assertEqual(score.score(self.root, io.StringIO()), 0.0)

    def test_counts_passed_stages(self):
        self.prove(build="pass", **{"host-test": "pass"}, qemu="fail")
        self.assertEqual(score.score(self.root, io.StringIO()), 66.7)

    def test_stale_proof_counts_nothing(self):
        self.prove(build="pass", **{"host-test": "pass"}, qemu="pass")
        (self.root / "examples" / "a" / "main.c").write_text("int y;")
        out = io.StringIO()
        self.assertEqual(score.score(self.root, out), 0.0)
        self.assertIn("stale", out.getvalue())

    def test_build_output_doesnt_change_the_hash(self):
        h = score.inputs_hash(self.root / "examples" / "a")
        (self.root / "examples" / "a" / "build").mkdir()
        (self.root / "examples" / "a" / "build" / "x.o").write_text("junk")
        (self.root / "examples" / "a" / "sdkconfig").write_text("CONFIG_X=y")
        self.assertEqual(score.inputs_hash(self.root / "examples" / "a"), h)


class Targets(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.projects = Path(tempfile.mkdtemp())
        (self.root / "proof").mkdir()
        (self.root / "targets.json").write_text(json.dumps(
            {"lite": {"project": "rx", "path": "firmware", "pins": {"idf": "v4.4.8"}}}))
        self.env = mock.patch.dict(os.environ, {"HIVE_PROJECTS": str(self.projects)})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def prove(self, inputs, **stages):
        (self.root / "proof" / "lite.json").write_text(json.dumps({"inputs": inputs, "stages": stages}))

    def test_unseen_target_is_judged_on_its_proof(self):
        self.prove("whatever", build="pass", qemu="pass")
        out = io.StringIO()
        self.assertEqual(score.score(self.root, out), 66.7)
        self.assertIn("not on this machine", out.getvalue())

    def test_seen_target_must_match_its_files(self):
        fw = self.projects / "rx" / "firmware"
        fw.mkdir(parents=True)
        (fw / "main.c").write_text("int x;")
        self.prove(score.inputs_hash(fw), build="pass", **{"host-test": "pass"}, qemu="pass")
        self.assertEqual(score.score(self.root, io.StringIO()), 100.0)
        (fw / "main.c").write_text("int y;")
        self.assertEqual(score.score(self.root, io.StringIO()), 0.0)


class Proofs(unittest.TestCase):
    """The module's own record holds: every example has a proof made on its
    current files. Edit an example, run `fw check-all NAME`, commit proof/."""

    def test_every_example_has_a_fresh_proof(self):
        for n in score.examples():
            _, why = score.proven(_util.ROOT, n, _util.ROOT / "examples" / n)
            self.assertEqual(why, "", f"examples/{n}: {why}")


if __name__ == "__main__":
    unittest.main()
