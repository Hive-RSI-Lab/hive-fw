import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import _util
import fuzz
import new

HELLO = _util.ROOT / "examples" / "hello"
FW = str(_util.ROOT / "bin" / "fw")


def copy_hello() -> Path:
    d = Path(tempfile.mkdtemp()) / "hello"
    shutil.copytree(HELLO, d, ignore=shutil.ignore_patterns("build", "build-*"))
    return d


class Templates(unittest.TestCase):
    def test_each_has_a_summary_and_a_host_test(self):
        have = new.templates()
        self.assertIn("plain", have)
        self.assertIn("espnow-link", have)
        self.assertIn("ble-link", have)
        for t, summary in have.items():
            self.assertTrue(summary, t)
            self.assertTrue(list((new.TEMPLATES / t / "host_test").glob("test_*.c")), t)

    def test_names(self):
        d = copy_hello()
        for bad in ("", "9x", "Knob", "a b", "x-", "x_", "a/b", "../x", "x" * 40):
            with self.assertRaises(ValueError, msg=bad):
                new.create("plain", bad, d)

    def test_idfs(self):
        for t in new.templates():
            self.assertTrue(all(v.startswith("v") for v in new.idfs(t)), t)
        self.assertIn("v5.5.6", new.idfs("espnow-link"))

    def test_check_project(self):
        d = new.check_project("espnow-link", "v5.5.6", Path(tempfile.mkdtemp()) / "p")
        self.assertEqual(json.loads((d / "fw.json").read_text())["idf"], "v5.5.6")
        self.assertTrue((d / "components" / "espnow-link" / "espnow_link_espnow.c").is_file())
        self.assertTrue((d / "test" / "host" / "test_espnow_link.c").is_file())

    def test_check_project_adds_sdkconfig(self):
        d = new.check_project("ble-link", "v4.4.8", Path(tempfile.mkdtemp()) / "p")
        self.assertTrue((d / "components" / "ble-link" / "ble_link_ble.c").is_file())
        self.assertIn("CONFIG_BT_ENABLED=y", (d / "sdkconfig.defaults").read_text().splitlines())

    def test_sdkconfig_added_once_and_never_over_the_projects(self):
        d = copy_hello()
        sd = d / "sdkconfig.defaults"
        sd.write_text(sd.read_text().rstrip("\n") + "\n# CONFIG_BT_GATTC_ENABLE is not set")  # no final newline
        added = new.add_sdkconfig("ble-link", "radio", d)
        self.assertIn("CONFIG_BT_ENABLED=y", added)
        self.assertNotIn("CONFIG_BT_GATTC_ENABLE=y", added)
        lines = sd.read_text().splitlines()
        self.assertIn("# CONFIG_BT_GATTC_ENABLE is not set", lines)
        self.assertIn("# radio (fw new, template ble-link)", lines)
        self.assertIn("CONFIG_ESPTOOLPY_FLASHSIZE_4MB=y", lines)
        before = sd.read_text()
        self.assertEqual(new.add_sdkconfig("ble-link", "radio", d), [])
        self.assertEqual(sd.read_text(), before)
        self.assertEqual(new.add_sdkconfig("plain", "knob", d), [])
        e = copy_hello()
        (e / "sdkconfig.defaults").unlink()
        self.assertTrue(new.add_sdkconfig("ble-link", "radio", e))
        self.assertTrue((e / "sdkconfig.defaults").read_text().startswith("# radio"))

    def test_sdkconfig_old_names_count_as_set(self):
        d = copy_hello()
        sd = d / "sdkconfig.defaults"
        sd.write_text(sd.read_text() + "CONFIG_BLUEDROID_ENABLED=y\n# CONFIG_GATTS_ENABLE is not set\n")
        added = new.add_sdkconfig("ble-link", "radio", d)
        self.assertIn("CONFIG_BT_GATTC_ENABLE=y", added)
        self.assertNotIn("CONFIG_BT_BLUEDROID_ENABLED=y", added)
        self.assertNotIn("CONFIG_BT_GATTS_ENABLE=y", added)

    def test_sdkconfig_choice_conflict_is_a_warning(self):
        d = copy_hello()
        sd = d / "sdkconfig.defaults"
        sd.write_text(sd.read_text() + "CONFIG_BT_NIMBLE_ENABLED=y\n")
        added = new.add_sdkconfig("ble-link", "radio", d)
        self.assertIn("CONFIG_BT_ENABLED=y", added)
        self.assertNotIn("CONFIG_BT_BLUEDROID_ENABLED=y", added)
        w = new.sdkconfig_warnings("ble-link", d)
        self.assertEqual(len(w), 1)
        self.assertIn("CONFIG_BT_NIMBLE_ENABLED", w[0])
        e = copy_hello()
        (e / "sdkconfig.defaults").write_text("CONFIG_NIMBLE_ENABLED=y\n")  # the old name
        self.assertTrue(new.sdkconfig_warnings("ble-link", e))
        self.assertEqual(new.sdkconfig_warnings("ble-link", copy_hello()), [])
        f = copy_hello()
        (f / "sdkconfig.defaults").write_text("# CONFIG_BT_NIMBLE_ENABLED is not set\n")
        self.assertEqual(new.sdkconfig_warnings("ble-link", f), [])

    def test_sdkconfig_goes_to_the_project_with_into(self):
        d = copy_hello()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = new.main(["component", "radio", str(d), "--from", "ble-link", "--into", str(d / "lib" / "radio")])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertTrue((d / "lib" / "radio" / "radio_ble.c").is_file())
        self.assertIn("CONFIG_BT_ENABLED=y", (d / "sdkconfig.defaults").read_text().splitlines())
        self.assertFalse((d / "lib" / "radio" / "sdkconfig.defaults").exists())

    def test_template_sdkconfig_lines_are_options(self):
        for t in new.templates():
            for ln in new.template_json(t).get("sdkconfig", []):
                self.assertTrue(new._key(ln), (t, ln))

    def test_check_unknown_template(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(new.main(["check", "nope"]), 2)

    def test_unknown_template(self):
        with self.assertRaises(ValueError):
            new.create("nope", "knob", copy_hello())


@unittest.skipUnless(shutil.which("cc"), "no C compiler")
class Instantiate(unittest.TestCase):
    def run_fw(self, *args):
        return subprocess.run([FW, *args], capture_output=True, text=True)

    def test_every_template_passes_its_host_test(self):
        for t in new.templates():
            with self.subTest(template=t):
                d = copy_hello()
                files = new.create(t, "my-part", d)
                self.assertTrue(all(f.is_file() for f in files))
                for f in files:
                    self.assertNotIn("@", f.name)
                    if "seeds" not in f.parts:
                        self.assertNotRegex(f.read_text(), r"@[A-Z]+@", f)
                self.assertTrue((d / "components" / "my-part" / "CMakeLists.txt").is_file())
                r = self.run_fw("host-test", str(d), "-k", "test_my_part")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn("1/1 test programs passed", r.stdout)

    def test_default_template_is_the_name(self):
        d = copy_hello()
        r = self.run_fw("new", "component", "espnow-link", str(d))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("from template espnow-link", r.stdout)
        self.assertTrue((d / "components" / "espnow-link" / "espnow_link_espnow.c").is_file())
        r = self.run_fw("new", "component", "knob", str(d))
        self.assertIn("from template plain", r.stdout)
        self.assertNotIn("CONFIG_BT_ENABLED", (d / "sdkconfig.defaults").read_text())
        (d / "sdkconfig").write_text("")
        r = self.run_fw("new", "component", "ble-link", str(d))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("sdkconfig.defaults: added CONFIG_BT_ENABLED=y", r.stdout)
        self.assertIn("set those in menuconfig too", r.stdout)
        self.assertIn("CONFIG_BT_ENABLED=y", (d / "sdkconfig.defaults").read_text())

    def test_into_and_host_tests_dir(self):
        d = copy_hello()
        pins = json.loads((d / "fw.json").read_text())
        pins["host_tests"] = "tests/unit"
        (d / "fw.json").write_text(json.dumps(pins))
        new.create("plain", "knob", d, into=d / "lib" / "knob2")
        t = d / "tests" / "unit" / "test_knob.c"
        self.assertTrue(t.is_file())
        self.assertIn("lib/knob2/knob.c", t.read_text())
        r = self.run_fw("host-test", str(d), "-k", "test_knob")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_into_outside_the_project_is_refused(self):
        d = copy_hello()
        with self.assertRaises(ValueError):
            new.create("plain", "knob", d, into=d.parent / "elsewhere")
        self.assertFalse((d.parent / "elsewhere").exists())

    def test_clash_needs_force(self):
        d = copy_hello()
        new.create("plain", "knob", d)
        c = d / "components" / "knob" / "knob.c"
        c.write_text("/* mine */\n")
        with self.assertRaises(ValueError):
            new.create("plain", "knob", d)
        self.assertEqual(c.read_text(), "/* mine */\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(new.main(["component", "knob", str(d)]), 2)
        self.assertIn("--force", err.getvalue())
        new.create("plain", "knob", d, force=True)
        self.assertNotEqual(c.read_text(), "/* mine */\n")

    def test_list(self):
        r = self.run_fw("new", "list")
        self.assertEqual(r.returncode, 0)
        self.assertIn("espnow-link", r.stdout)
        self.assertIn("plain", r.stdout)


@unittest.skipUnless(fuzz.find_clang(), "no clang that links libFuzzer")
class Fuzz(unittest.TestCase):
    def run_fw(self, *args):
        return subprocess.run([FW, *args], capture_output=True, text=True)

    def test_espnow_link_harness_and_its_regressions(self):
        d = copy_hello()
        new.create("espnow-link", "my-part", d)
        f = d / "test" / "fuzz"
        self.assertTrue((f / "fuzz_my_part.c").is_file())
        src = new.TEMPLATES / "espnow-link" / "fuzz" / "seeds" / "fuzz_@NAME@"
        seeds = f / "seeds" / "fuzz_my_part"
        for s in src.iterdir():  # inputs are copied byte for byte, not filled
            self.assertEqual((seeds / s.name).read_bytes(), s.read_bytes(), s.name)
        r = self.run_fw("fuzz", str(d), "--only", "fuzz_my_part", "--secs", "3")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        c = d / "components" / "my-part" / "my_part.c"
        fixed = c.read_text()
        # each regression input fails with its bug put back (the sanitizer or
        # an invariant, not some other error), and only then
        bugs = {
            "regress-seq-wrap": ("\tif (seq == UINT32_MAX) return -1;\n", "", "FUZZ_ASSERT"),
            "regress-pong-time-overflow": ("if (f->t1 < 0 || f->t1 > t4 || t4 > MY_PART_T_MAX || f->t2 < 0 || "
                                           "f->t3 < f->t2 || f->t3 > MY_PART_T_MAX) {", "if (0) {",
                                           "runtime error"),
        }
        try:
            for name, (fix, bug, says) in bugs.items():
                with self.subTest(regress=name):
                    self.assertIn(fix, fixed)
                    r = self.run_fw("fuzz", str(d), "--replay", "fuzz_my_part", str(seeds / name))
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    c.write_text(fixed.replace(fix, bug))
                    r = self.run_fw("fuzz", str(d), "--replay", "fuzz_my_part", str(seeds / name))
                    self.assertNotEqual(r.returncode, 0, name)
                    self.assertIn(says, r.stdout + r.stderr, name)
                    c.write_text(fixed)
        finally:
            c.write_text(fixed)

if __name__ == "__main__":
    unittest.main()
