import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import _util
import hosttest

HELLO = _util.ROOT / "examples" / "hello"


class Directives(unittest.TestCase):
    def test_reads_header_lines(self):
        t = Path(tempfile.mkdtemp()) / "test_x.c"
        t.write_text('// fw-src: main/a.c "comp/b c.c"\n// fw-inc: main\n// fw-cflags: -DX=1\n#include "unity.h"\n')
        d = hosttest.directives(t)
        self.assertEqual(d["src"], ["main/a.c", "comp/b c.c"])
        self.assertEqual(d["inc"], ["main"])
        self.assertEqual(d["cflags"], ["-DX=1"])


@unittest.skipUnless(shutil.which("cc"), "no C compiler")
class Simple(unittest.TestCase):
    def copy(self) -> Path:
        d = Path(tempfile.mkdtemp()) / "hello"
        shutil.copytree(HELLO, d, ignore=shutil.ignore_patterns("build"))
        return d

    def test_example_passes(self):
        d = self.copy()
        r = subprocess.run([str(_util.ROOT / "bin" / "fw"), "host-test", str(d)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("1/1 test programs passed", r.stdout)

    def test_a_failing_assert_fails(self):
        d = self.copy()
        src = d / "main" / "volume.c"
        src.write_text(src.read_text().replace("return 192;", "return 191;"))
        r = subprocess.run([str(_util.ROOT / "bin" / "fw"), "host-test", str(d)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAIL", r.stdout)

    def test_a_build_error_fails(self):
        d = self.copy()
        (d / "test" / "host" / "test_broken.c").write_text("int main(void) { return nope; }\n")
        r = subprocess.run([str(_util.ROOT / "bin" / "fw"), "host-test", str(d)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("test_broken.c: BUILD FAILED", r.stdout)


if __name__ == "__main__":
    unittest.main()
