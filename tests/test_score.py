import io
import json
import tempfile
import unittest
from pathlib import Path

from tests import _util
import score


class Score(unittest.TestCase):
    """A one-file corpus: the checker reads a partition table."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.b = self.root / "faults" / "x"
        self.b.mkdir(parents=True)
        (self.b / "fw.json").write_text(json.dumps({"constraints": {"partitions": "p.csv"}}))
        (self.b / "p.csv").write_text("nvs, data, nvs, 0x9000, 0x6000,\nfactory, app, factory, 0x10000, 0x100000,\n")

    def faults(self, *fs):
        (self.b / "faults.json").write_text(json.dumps({"faults": [
            {"id": i, "edits": [{"file": "p.csv", "find": a, "replace": b}]} for i, a, b in fs]}))

    def test_caught_missed_and_broken(self):
        self.faults(("overlap", "0x6000", "0x8000"), ("missed", "nvs, data", "nvs,  data"),
                    ("broken", "no such text", "x"))
        out = io.StringIO()
        r = score.run(self.root, out)
        self.assertEqual((r["caught"], r["faults"], r["false_alarms"]), (1, 3, 0))
        self.assertIn("x/overlap: caught by PART-OVERLAP", out.getvalue())
        self.assertIn("x/missed: missed", out.getvalue())
        self.assertIn("x/broken: BROKEN", out.getvalue())
        self.assertEqual(score.score(self.root, io.StringIO()), 1)

    def test_a_false_alarm_on_the_clean_base_costs_a_point(self):
        (self.b / "p.csv").write_text("nvs, data, nvs, 0x8000, 0x6000,\nfactory, app, factory, 0x10000, 0x100000,\n")
        self.faults(("ota", "factory, app, factory", "ota_0, app, ota_0"))
        out = io.StringIO()
        self.assertEqual(score.score(self.root, out), 0)  # caught 1, 1 false alarm
        self.assertIn("false alarm on the clean base", out.getvalue())

    def test_an_error_the_base_already_has_doesnt_count_as_caught(self):
        (self.b / "p.csv").write_text("nvs, data, nvs, 0x8000, 0x6000,\n")
        self.faults(("same", "0x6000", "0x6001"))  # still over the table, nothing new
        r = score.run(self.root, io.StringIO())
        self.assertEqual((r["caught"], r["false_alarms"]), (0, 2))


class Corpus(unittest.TestCase):
    """The module's own corpus: every fault plants cleanly, no clean base
    raises an error, and each fault caught is caught by the check it's for."""

    def test_the_corpus_holds(self):
        r = score.run(_util.ROOT, io.StringIO())
        self.assertEqual(r["false_alarms"], 0)
        self.assertEqual([x["id"] for x in r["results"] if x["broken"]], [])
        expect = {f["id"]: f["expect"] for b in score.bases()
                  for f in json.loads((b / "faults.json").read_text())["faults"]}
        for x in r["results"]:
            if x["caught"]:
                self.assertIn(expect[x["id"]], x["by"], x["id"])


if __name__ == "__main__":
    unittest.main()
