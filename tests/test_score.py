import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

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


class Proofs(unittest.TestCase):
    """The module's own record holds: every example has a proof made on its
    current files. Edit an example, run `fw check-all NAME`, commit proof/."""

    def test_every_example_has_a_fresh_proof(self):
        for n in score.examples():
            _, why = score.proven(_util.ROOT, n)
            self.assertEqual(why, "", f"examples/{n}: {why}")


if __name__ == "__main__":
    unittest.main()
