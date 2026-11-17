import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import _util
import coverage

HELLO = _util.ROOT / "examples" / "hello"
FW = str(_util.ROOT / "bin" / "fw")


class Merge(unittest.TestCase):
    def test_two_programs_sharing_a_file_count_once(self):
        root = Path("/p")
        rec = lambda counts, calls: {"file": "main/a.c", "lines": [
            {"line_number": n, "count": c, "branches": [{"count": c}, {"count": 0}]} for n, c in counts],
            "functions": [{"name": "f", "start_line": 1, "execution_count": calls}]}
        files = coverage.merge([rec([(1, 1), (2, 0)], 1), rec([(1, 0), (2, 3)], 0)], root)
        rows = coverage.summarize(files, root, lambda p: True)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["lines"], r["lines_run"]), (2, 2))
        self.assertEqual((r["branches"], r["branches_taken"]), (4, 2))
        self.assertEqual((r["functions"], r["functions_run"]), (1, 1))
        self.assertEqual(coverage.totals(rows)["line_pct"], 100.0)

    def test_ranges(self):
        self.assertEqual(coverage.ranges([3, 4, 5, 9, 11, 12]), "3-5,9,11-12")
        self.assertEqual(coverage.ranges([]), "")


@unittest.skipUnless(shutil.which("gcc") and shutil.which("gcov"), "no gcc/gcov")
class Hello(unittest.TestCase):
    def copy(self) -> Path:
        d = Path(tempfile.mkdtemp()) / "hello"
        shutil.copytree(HELLO, d, ignore=shutil.ignore_patterns("build*"))
        return d

    def run_fw(self, *args):
        return subprocess.run([FW, "coverage", *args], capture_output=True, text=True)

    def test_example_reports_its_source(self):
        d = self.copy()
        r = self.run_fw(str(d), "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        files = {f["file"]: f for f in out["files"]}
        self.assertEqual(list(files), ["main/volume.c"], files)
        self.assertGreater(files["main/volume.c"]["lines_run"], 0)
        self.assertEqual(out["failed"], [])

    def test_min_fails_when_under(self):
        d = self.copy()
        r = self.run_fw(str(d), "--min", "100.1")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("under --min", r.stderr)
        self.assertIn("main/volume.c", r.stdout)

    def test_unrun_function_is_named(self):
        d = self.copy()
        src = d / "main" / "volume.c"
        src.write_text(src.read_text() + "\nint fw_cov_never(int x) { return x + 1; }\n")
        r = self.run_fw(str(d), "--lines")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("never called: fw_cov_never", r.stdout)
        self.assertIn("lines not run:", r.stdout)

    def test_a_failing_test_fails(self):
        d = self.copy()
        src = d / "main" / "volume.c"
        src.write_text(src.read_text().replace("return 192;", "return 191;"))
        r = self.run_fw(str(d))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("failed to build or run: test_volume.c", r.stdout)

    def test_makefile_without_coverage_target(self):
        d = self.copy()
        (d / "test" / "host" / "Makefile").write_text("all:\n\ttrue\n")
        r = self.run_fw(str(d))
        self.assertEqual(r.returncode, 2)
        self.assertIn("no coverage target", r.stderr)


if __name__ == "__main__":
    unittest.main()
