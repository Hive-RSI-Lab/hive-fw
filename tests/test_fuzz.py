import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import _util
import fuzz

HELLO = _util.ROOT / "examples" / "hello"
FW = str(_util.ROOT / "bin" / "fw")
CLANG = fuzz.find_clang()


def copy() -> Path:
    d = Path(tempfile.mkdtemp()) / "hello"
    shutil.copytree(HELLO, d, ignore=shutil.ignore_patterns("build*"))
    return d


class Parts(unittest.TestCase):
    def test_dictionary_takes_literals_not_formats(self):
        src = Path(tempfile.mkdtemp()) / "a.c"
        src.write_text('/* "in a comment" */\nif (!strcmp(s, "volume")) printf("%d", x);\n'
                       'route("/api/v1/info"); char c = "x"[0];\n')
        d = fuzz.dictionary([src])
        self.assertIn('"volume"', d)
        self.assertIn('"/api/v1/info"', d)
        self.assertNotIn('"%d"', d)
        self.assertNotIn('"x"', d)
        self.assertFalse(any("comment" in e for e in d))

    def test_new_writes_template_once(self):
        d = copy()
        r = subprocess.run([FW, "fuzz", str(d), "--new", "parser"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        h = d / "test" / "fuzz" / "fuzz_parser.c"
        self.assertIn("LLVMFuzzerTestOneInput", h.read_text())
        self.assertIn("// fw-src: main/parser.c", h.read_text())
        self.assertTrue((d / "test" / "fuzz" / "seeds" / "fuzz_parser").is_dir())
        r = subprocess.run([FW, "fuzz", str(d), "--new", "parser"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)

    def test_no_harness(self):
        d = Path(tempfile.mkdtemp())
        r = subprocess.run([FW, "fuzz", str(d)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn("no fuzz_*.c", r.stderr)


@unittest.skipUnless(CLANG, "no clang with libFuzzer")
class Run(unittest.TestCase):
    def test_example_runs_clean(self):
        d = copy()
        r = subprocess.run([FW, "fuzz", str(d), "--secs", "2"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"fuzz fuzz_volume \(2 s\): \d+ runs, coverage \d+, no crash")

    def test_planted_bug_is_caught_and_replays(self):
        d = copy()
        src = d / "main" / "volume.c"
        # a louder percent past 100 now attenuates again: the monotone check trips
        src.write_text(src.read_text().replace("if (percent >= 100) {\n        return 0;",
                                               "if (percent >= 100) {\n        return percent > 150 ? 7 : 0;"))
        r = subprocess.run([FW, "fuzz", str(d), "--secs", "20"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("fuzz fuzz_volume: CRASH", r.stdout)
        self.assertIn("FUZZ_ASSERT", r.stdout)
        crashes = sorted((d / "build-fuzz").glob("crash-fuzz_volume-*"))
        self.assertTrue(crashes)
        r = subprocess.run([FW, "fuzz", str(d), "--replay", "volume", str(crashes[0])],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)

    def test_build_failure_is_reported(self):
        d = copy()
        h = d / "test" / "fuzz" / "fuzz_volume.c"
        h.write_text(h.read_text().replace("return 0;\n}", "return 0\n}"))
        r = subprocess.run([FW, "fuzz", str(d), "--secs", "1"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("fuzz_volume: BUILD FAILED", r.stdout)


if __name__ == "__main__":
    unittest.main()
