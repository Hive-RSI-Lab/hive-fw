import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
import shutil
from pathlib import Path

from unittest import mock

from tests import _util
import fwlib
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


def git(*args, cwd=None):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main",
                           "-c", "protocol.file.allow=always", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout


def repo(d: Path, files: dict) -> Path:
    d.mkdir(parents=True)
    git("init", "-q", cwd=d)
    for k, v in files.items():
        (d / k).write_text(v)
    git("add", "-A", cwd=d)
    git("commit", "-qm", "x", cwd=d)
    return d


@unittest.skipUnless(shutil.which("git"), "no git")
class Submodules(unittest.TestCase):
    """A superproject with a submodule git can fetch (which has its own
    nested one) and one on github.com that git can't: that one comes from the
    codeload tarball of its pinned commit, the way the proxy forces for
    IDF's nghttp2."""

    @classmethod
    def setUpClass(cls):
        cls.env = mock.patch.dict(os.environ, {
            "GIT_ALLOW_PROTOCOL": "file",  # https fails at once, with no network
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.file.allow", "GIT_CONFIG_VALUE_0": "always"})
        cls.env.start()
        cls.t = Path(tempfile.mkdtemp())
        inner = repo(cls.t / "inner", {"i.txt": "inner"})
        ok = repo(cls.t / "ok", {"ok.txt": "ok"})
        git("submodule", "add", "-q", str(inner), "deep", cwd=ok)
        git("commit", "-qm", "sub", cwd=ok)
        cls.gh = repo(cls.t / "gh", {"CMakeLists.txt": "gh"})
        cls.gh_sha = git("rev-parse", "HEAD", cwd=cls.gh).strip()
        cls.top = repo(cls.t / "top", {"README": "top"})
        git("submodule", "add", "-q", str(ok), "components/ok", cwd=cls.top)
        git("submodule", "add", "-q", str(cls.gh), "components/nghttp/nghttp2", cwd=cls.top)
        git("config", "-f", ".gitmodules", "submodule.components/nghttp/nghttp2.url",
            "https://github.com/nghttp2/nghttp2.git", cwd=cls.top)
        git("commit", "-qam", "subs", cwd=cls.top)

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def setUp(self):
        self.src = Path(tempfile.mkdtemp()) / "src"
        git("clone", "-q", str(self.top), str(self.src))
        self.urls = []
        quiet = mock.patch.object(setup, "log", lambda msg: None)
        quiet.start()
        self.addCleanup(quiet.stop)

    def fake_download(self, url, dest):
        self.urls.append(url)
        with tarfile.open(dest, "w:gz") as tf:
            tf.add(self.gh / "CMakeLists.txt", arcname=f"nghttp2-{self.gh_sha}/CMakeLists.txt")
        return dest

    def test_refused_submodule_comes_from_codeload(self):
        with mock.patch.object(setup, "download", self.fake_download):
            setup.fill_submodules(self.src)
            self.assertEqual(self.urls, [f"https://codeload.github.com/nghttp2/nghttp2/tar.gz/{self.gh_sha}"])
            self.assertEqual((self.src / "components/ok/ok.txt").read_text(), "ok")
            self.assertEqual((self.src / "components/ok/deep/i.txt").read_text(), "inner")
            self.assertEqual((self.src / "components/nghttp/nghttp2/CMakeLists.txt").read_text(), "gh")
            self.assertEqual(git("config", "submodule.components/nghttp/nghttp2.update", cwd=self.src).strip(), "none")
            # A second run (or git's own update) leaves it be.
            setup.fill_submodules(self.src)
            git("submodule", "update", "--init", "--recursive", cwd=self.src)
        self.assertEqual(len(self.urls), 1)

    def test_no_tarball_for_other_hosts(self):
        git("config", "-f", ".gitmodules", "submodule.components/nghttp/nghttp2.url",
            "https://gitlab.com/x/y.git", cwd=self.src)
        git("submodule", "sync", "-q", cwd=self.src)
        with mock.patch.object(setup, "download", self.fake_download):
            with self.assertRaisesRegex(RuntimeError, "no tarball"):
                setup.fill_submodules(self.src)
        self.assertEqual(self.urls, [])

    def test_codeload_url(self):
        self.assertEqual(setup.codeload_url("git@github.com:a/b.git", "s"), "https://codeload.github.com/a/b/tar.gz/s")
        self.assertIsNone(setup.codeload_url("https://gitlab.com/a/b.git", "s"))


class Pins(unittest.TestCase):
    def test_finds_fw_json_in_a_subfolder(self):
        d = Path(tempfile.mkdtemp())
        (d / "firmware").mkdir()
        (d / "firmware" / "fw.json").write_text(json.dumps({"idf": "v4.4.8", "target": "esp32"}))
        (d / ".checkouts" / "x").mkdir(parents=True)
        (d / ".checkouts" / "x" / "fw.json").write_text(json.dumps({"idf": "v5.1.4"}))
        got = setup.project_pins(d)
        self.assertEqual([(w.name, p["idf"]) for w, p in got], [("firmware", "v4.4.8")])
        (d / "fw.json").write_text(json.dumps({"idf": "v5.2.1"}))
        self.assertEqual([p["idf"] for _, p in setup.project_pins(d)], ["v5.2.1"])

    def test_setup_takes_project_positionally(self):
        d = Path(tempfile.mkdtemp())
        (d / "firmware").mkdir()
        (d / "firmware" / "fw.json").write_text(json.dumps({"idf": "v4.4.8", "target": "esp32s3"}))
        calls = []
        with mock.patch.object(fwlib, "find_idf", lambda idf: None), \
             mock.patch.object(fwlib, "idf_ready", lambda root, idf: False), \
             mock.patch.object(setup, "install_idf", lambda prefix, idf, t: calls.append((idf, t))), \
             mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(setup.main([str(d), "--prefix", str(d / "p"), "--no-qemu"]), 0)
        self.assertEqual(calls, [("v4.4.8", "esp32s3")])


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
