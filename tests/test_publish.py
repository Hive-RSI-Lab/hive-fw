import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import _util  # noqa: F401
import publish

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "publish.py"


def project(fw=None, bench=False, build=True, name="My_Hello App"):
    d = Path(tempfile.mkdtemp()) / name
    d.mkdir()
    (d / "fw.json").write_text(json.dumps(fw if fw is not None else {"idf": "v4.4.8", "target": "esp32s3"}))
    if build:
        (d / "build").mkdir()
        (d / "build" / "flash_args").write_text("--flash_mode dio --flash_freq 40m --flash_size 4MB\n0x10000 hello.bin\n")
    if bench:
        (d / "bench.json").write_text("{}")
    return d


class Names(unittest.TestCase):
    def test_bench_name_from_fw_json_or_folder(self):
        self.assertEqual(publish.bench_name({"name": "Hifi Lite"}, Path("/x")), "hifi-lite")
        self.assertEqual(publish.bench_name({}, Path("/x/My_Hello App")), "my-hello-app")
        with self.assertRaises(ValueError):
            publish.bench_name({"name": "__"}, Path("/x"))

    def test_chip_as_esptool_names_it(self):
        self.assertEqual(publish.chip_of("esp32"), "ESP32")
        self.assertEqual(publish.chip_of("esp32s3"), "ESP32-S3")
        self.assertEqual(publish.chip_of("esp32c3"), "ESP32-C3")
        with self.assertRaises(ValueError):
            publish.chip_of("stm32f4")


class Plan(unittest.TestCase):
    def test_command(self):
        d = project(bench=True)
        cmd, bdir = publish.plan(d, ["--", "--label", "Hello"])
        self.assertEqual(bdir, d / "build")
        self.assertEqual(cmd, ["publish", str(d / "build"), "my-hello-app", "--chip", "ESP32-S3",
                               "--profile", str(d / "bench.json"), "--post", "--label", "Hello"])

    def test_no_post_no_profile_and_a_name(self):
        d = project()
        cmd, _ = publish.plan(d, ["--no-post", "--name", "hello"])
        self.assertEqual(cmd, ["publish", str(d / "build"), "hello", "--chip", "ESP32-S3"])

    def test_bench_key_must_exist(self):
        d = project(fw={"target": "esp32", "bench": "board/bench.json"})
        with self.assertRaisesRegex(FileNotFoundError, "bench"):
            publish.plan(d, [])

    def test_no_build_says_build_first(self):
        d = project(build=False)
        with self.assertRaisesRegex(FileNotFoundError, "fw build"):
            publish.plan(d, [])

    def test_unknown_argument(self):
        with self.assertRaises(ValueError):
            publish.plan(project(), ["--label", "x"])


class Run(unittest.TestCase):
    def run_fw(self, d, env):
        return subprocess.run([sys.executable, str(SCRIPT), str(d)], env=env, text=True, capture_output=True)

    def test_without_a_bench_says_so(self):
        d = project()
        env = {k: v for k, v in os.environ.items() if k != "FW_SERIALTAB"}
        env["PATH"] = tempfile.mkdtemp()  # no serialtab on it
        r = self.run_fw(d, env)
        self.assertEqual(r.returncode, publish.NO_BENCH)
        self.assertIn("serialtab isn't installed", r.stderr)
        self.assertIn(str(d / "build"), r.stderr)

    def test_calls_the_bench(self):
        d = project(bench=True)
        log = Path(tempfile.mkdtemp()) / "args"
        fake = log.parent / "serialtab"
        fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {log}\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        with mock.patch.dict(os.environ, {"FW_SERIALTAB": str(fake)}):
            r = self.run_fw(d, dict(os.environ))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(log.read_text().split("\n")[:5], ["publish", str(d / "build"), "my-hello-app", "--chip", "ESP32-S3"])
        self.assertIn("--post", log.read_text())


if __name__ == "__main__":
    unittest.main()
