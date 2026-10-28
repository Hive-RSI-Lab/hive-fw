import subprocess
import tarfile
import tempfile
import unittest
import shutil
from pathlib import Path

from tests import _util
import setup


@unittest.skipUnless(shutil.which("ar"), "no ar")
class Deb(unittest.TestCase):
    def test_unpacks_shared_libs_flat(self):
        t = Path(tempfile.mkdtemp())
        root = t / "root" / "usr" / "lib" / "x86_64-linux-gnu"
        root.mkdir(parents=True)
        (root / "libslirp.so.0.4.0").write_bytes(b"ELF")
        (root / "libslirp.so.0").symlink_to("libslirp.so.0.4.0")
        (t / "root" / "usr" / "share").mkdir()
        (t / "root" / "usr" / "share" / "README").write_text("x")
        with tarfile.open(t / "data.tar.xz", "w:xz") as tf:
            tf.add(t / "root", arcname=".")
        (t / "debian-binary").write_text("2.0\n")
        pkg = t / "pkg" / "x.deb"
        pkg.parent.mkdir()
        subprocess.run(["ar", "rc", str(pkg), str(t / "debian-binary"), str(t / "data.tar.xz")], check=True)
        lib = t / "lib"
        setup.unpack_deb_libs(pkg, lib)
        self.assertEqual(sorted(p.name for p in lib.iterdir()), ["libslirp.so.0", "libslirp.so.0.4.0"])
        self.assertEqual((lib / "libslirp.so.0").read_bytes(), b"ELF")


class Cli(unittest.TestCase):
    def test_entry_point_is_executable_in_git(self):
        # A clean clone must get +x: the project's test command is `bin/fw test`.
        r = subprocess.run(["git", "-C", str(_util.ROOT), "ls-files", "-s", "bin/fw"],
                           capture_output=True, text=True)
        if r.returncode or not r.stdout:
            self.skipTest("not a git checkout")
        self.assertTrue(r.stdout.startswith("100755"), r.stdout)

    def test_help_and_unknown(self):
        fw = str(_util.ROOT / "bin" / "fw")
        r = subprocess.run([fw, "help"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("fw host-test", r.stdout)
        self.assertEqual(subprocess.run([fw, "nope"], capture_output=True).returncode, 2)

    def test_doctor_reports_missing_idf(self):
        d = Path(tempfile.mkdtemp())
        (d / "fw.json").write_text('{"idf": "v9.9.9"}')
        r = subprocess.run([str(_util.ROOT / "bin" / "fw"), "doctor", str(d)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("MISSING", r.stdout)


if __name__ == "__main__":
    unittest.main()
