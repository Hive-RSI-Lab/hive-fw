import json
import os
import re
import struct
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests import _util  # noqa: F401
import fwlib
import qemu

# A stand-in for qemu-system-xtensa: prints an IDF 4 boot, then serves the
# hifi test API's /api/v1/info and /api/v1/volume on the port fw forwards
# (hostfwd in -nic), after a delay like DHCP's.
FAKE_QEMU = textwrap.dedent("""\
    import http.server, json, re, sys, threading, time
    nic = sys.argv[sys.argv.index("-nic") + 1]
    print("I (10) cpu_start: Starting scheduler on PRO CPU.", flush=True)
    m = re.search(r"hostfwd=tcp:127.0.0.1:(\\d+)-:80", nic)
    if not m:
        time.sleep(60)
    vol = {"volume": 35}
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def reply(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code); self.send_header("Content-Length", str(len(b))); self.end_headers()
            self.wfile.write(b)
        def do_GET(self):
            if self.path == "/api/v1/info":
                self.reply(200, {"model": "hifi-lite", "api": 1})
            else:
                self.reply(404, {"error": "no such path"})
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if not 0 <= body.get("volume", 0) <= 100:
                return self.reply(400, {"error": "volume"})
            vol.update(body)
            self.reply(200, {"audio": vol})
    time.sleep(0.3)
    s = http.server.HTTPServer(("127.0.0.1", int(m.group(1))), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    print("I (900) hifi: http up", flush=True)
    time.sleep(60)
""")


def fake_build(d: Path, files: dict, size="4MB", chip="esp32") -> Path:
    fa = {"flash_settings": {"flash_size": size}, "flash_files": {},
          "extra_esptool_args": {"chip": chip}}
    for off, (name, data) in files.items():
        (d / name).parent.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(data)
        fa["flash_files"][off] = name
    (d / "flasher_args.json").write_text(json.dumps(fa))
    return d


class Merge(unittest.TestCase):
    def test_lays_files_at_offsets(self):
        d = fake_build(Path(tempfile.mkdtemp()), {
            "0x1000": ("bootloader/bootloader.bin", b"BOOT"),
            "0x8000": ("partition_table/partition-table.bin", b"PT"),
            "0x10000": ("hello.bin", b"APP" * 10)})
        img, chip = qemu.merge(d, d / "out.bin")
        b = img.read_bytes()
        self.assertEqual(len(b), 4 << 20)
        self.assertEqual(chip, "esp32")
        self.assertEqual(b[0x1000:0x1004], b"BOOT")
        self.assertEqual(b[0x8000:0x8002], b"PT")
        self.assertEqual(b[0x10000:0x1001e], b"APP" * 10)
        self.assertEqual(b[0:0x1000], b"\xff" * 0x1000)

    def test_detect_size_falls_back_to_4mb(self):
        d = fake_build(Path(tempfile.mkdtemp()), {"0x1000": ("b.bin", b"x")}, size="detect")
        img, _ = qemu.merge(d, d / "out.bin")
        self.assertEqual(img.stat().st_size, 4 << 20)

    def test_overflow_refused(self):
        d = fake_build(Path(tempfile.mkdtemp()), {"0x1ff000": ("a.bin", b"x" * 0x2000)}, size="2MB")
        with self.assertRaises(ValueError):
            qemu.merge(d, d / "out.bin")

    def test_unknown_chip_is_a_verdict(self):
        v = qemu.run(Path("/nonexistent"), Path("/x"), "esp32c6", "x", 1, 1, None, None)
        self.assertFalse(v.ok)
        self.assertIn("esp32c6", v.reason)


class Probes(unittest.TestCase):
    def test_parse_forms(self):
        p = qemu.parse_probe({"post": "/api/v1/volume", "json": {"volume": 40}, "status": 200})
        self.assertEqual((p["method"], p["path"], p["body"]), ("POST", "/api/v1/volume", b'{"volume": 40}'))
        p = qemu.parse_probe("POST /api/v1/volume {\"mute\": true}")
        self.assertEqual((p["method"], p["body"], p["status"]), ("POST", b'{"mute": true}', 200))
        self.assertIsNone(qemu.parse_probe("GET /api/v1/info")["body"])

    def test_parse_refuses(self):
        for bad in ("GET", "GET api/v1/info", {"get": "/a", "post": "/b"}, {"path": "/a"}):
            with self.assertRaises(ValueError, msg=bad):
                qemu.parse_probe(bad)


class HttpOverOpenEth(unittest.TestCase):
    """run() end to end against FAKE_QEMU: the port forward, the wait for the
    server, the probes' verdicts, and the transcript in the log."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.q = self.d / "bin" / "qemu-system-xtensa"
        self.q.parent.mkdir()
        (self.d / "fake.py").write_text(FAKE_QEMU)
        self.q.write_text(f"#!/bin/sh\nexec {sys.executable} -I {self.d / 'fake.py'} \"$@\"\n")
        self.q.chmod(0o755)
        self.log = self.d / "log"

    def boot(self, probes, **kw):
        return qemu.run(self.q, self.d / "img", "esp32", "cpu_start: Starting scheduler", 30, 0.5,
                        None, self.log, [qemu.parse_probe(p) for p in probes], **kw)

    def test_probes_pass(self):
        v = self.boot([{"get": "/api/v1/info", "expect": '"model": "hifi-lite"'},
                       {"post": "/api/v1/volume", "json": {"volume": 40}, "expect": '"volume": 40'},
                       {"post": "/api/v1/volume", "json": {"volume": 400}, "status": 400}])
        self.assertTrue(v.ok, v.reason)
        self.assertIn("3 request(s)", v.reason)
        log = self.log.read_text()
        self.assertIn("cpu_start", log)
        self.assertIn("POST /api/v1/volume -> 400 ok", log)

    def test_wrong_reply_fails(self):
        v = self.boot([{"get": "/api/v1/info", "expect": '"model": "hifi-ultra"'}])
        self.assertFalse(v.ok)
        self.assertIn("doesn't match", v.reason)

    def test_wrong_status_fails(self):
        v = self.boot([{"get": "/api/v1/info"}, {"get": "/api/v1/nope"}])
        self.assertFalse(v.ok)
        self.assertIn("GET /api/v1/nope: status 404, want 200", v.reason)

    def test_no_server_times_out(self):
        v = qemu.run(self.q, self.d / "img", "esp32", "cpu_start", 2, 0.5, None, self.log,
                     [qemu.parse_probe("GET /api/v1/info")], guest_port=81)
        self.assertFalse(v.ok)
        self.assertIn("http", v.reason.lower())

    def test_no_probes_no_forward(self):
        v = qemu.run(self.q, self.d / "img", "esp32", "cpu_start", 10, 0.5, None, self.log)
        self.assertTrue(v.ok, v.reason)


def ptable(*rows) -> bytes:
    """A binary partition table: (name, type, subtype, offset, size) rows."""
    t = b"".join(struct.pack("<HBBII16sI", 0x50AA, ty, st, off, size, name.encode(), 0)
                 for name, ty, st, off, size in rows)
    return t + b"\xeb\xeb" + b"\xff" * 30


class Shims(unittest.TestCase):
    def test_wraps_match_the_shims(self):
        src = fwlib.OPENETH_SHIM.read_text()
        defined = set(re.findall(r"__wrap_(esp_eth_\w+)\(", src))
        defined |= {f"esp_eth_phy_new_{n}" for n in re.findall(r"^FW_PHY\((\w+)\)", src, re.M)}
        self.assertEqual(defined, set(fwlib.OPENETH_WRAPS))
        src = fwlib.NOWIFI_SHIM.read_text()
        self.assertEqual(set(re.findall(r"^FW_OK\((\w+)\)", src, re.M)), set(fwlib.NOWIFI_WRAPS))
        self.assertIn("__wrap_adc1_get_raw", fwlib.ADC_SHIM.read_text())

    def test_shims_make_a_qemu_build(self):
        for q in ({"eth": "emac"}, {"adc1": 0}, {"wifi": "off"}):
            self.assertTrue(fwlib.qemu_variant({"qemu": q}), q)
        self.assertFalse(fwlib.qemu_variant({"qemu": {"http": [{"get": "/"}], "boot": "ota_0"}}))
        self.assertEqual(fwlib.qemu_build_dir(Path("/s"), {"qemu": {"eth": "emac"}}), Path("/s/build-qemu"))

    def test_adc1_values(self):
        self.assertEqual(fwlib.adc1_raw(0), [0] * 8)
        self.assertEqual(fwlib.adc1_raw({"6": 1700}), [0, 0, 0, 0, 0, 0, 1700, 0])
        for bad in (True, "0", {"8": 1}, {"6": 5000}, {"x": 1}, -1):
            with self.assertRaises(ValueError, msg=bad):
                fwlib.adc1_raw(bad)


class Flash(unittest.TestCase):
    def setUp(self):
        self.b = Path(tempfile.mkdtemp())
        (self.b / "partition_table").mkdir()
        (self.b / "partition_table" / "partition-table.bin").write_bytes(ptable(
            ("nvs", 1, 2, 0x9000, 0x4000), ("otadata", 1, 0, 0xD000, 0x2000),
            ("factory", 0, 0, 0x10000, 0x100000), ("ota_1", 0, 0x11, 0x110000, 0x100000),
            ("settings", 1, 2, 0x210000, 0x10000)))

    def test_partitions(self):
        p = qemu.partitions(self.b)
        self.assertEqual(list(p), ["nvs", "otadata", "factory", "ota_1", "settings"])
        self.assertEqual(p["ota_1"], (0, 0x11, 0x110000, 0x100000))

    def test_otadata_boots_the_slot(self):
        [(off, e)] = qemu.otadata(qemu.partitions(self.b), "ota_1")
        self.assertEqual(off, 0xD000)
        seq, _, state, crc = struct.unpack("<I20sII", e)
        self.assertEqual((seq, state), (2, 0xFFFFFFFF))
        # IDF's crc for ota_seq 1 is 0x4743989a (its own test data); ours is the same function
        self.assertEqual(qemu.zlib.crc32(struct.pack("<I", 1), 0xFFFFFFFF), 0x4743989A)
        self.assertEqual(crc, qemu.zlib.crc32(struct.pack("<I", 2), 0xFFFFFFFF))
        for bad in ("factory", "nvs", "ota_9"):
            with self.assertRaises(ValueError, msg=bad):
                qemu.otadata(qemu.partitions(self.b), bad)

    def test_nvs_csv(self):
        c = qemu.nvs_csv({"config": {"eth_config": "model=LAN8720,mdc=23,mdio=18", "n": 3}})
        self.assertEqual(c.splitlines(), ["key,type,encoding,value", "config,namespace,,",
                                          'eth_config,data,string,"model=LAN8720,mdc=23,mdio=18"',
                                          "n,data,i32,3"])
        with self.assertRaises(ValueError):
            qemu.nvs_csv({"config": {"x": [1]}})

    def test_merge_lays_extra_on_top(self):
        d = fake_build(self.b, {"0x1000": ("b.bin", b"B" * 16)})
        img, _ = qemu.merge(d, d / "out.bin", extra=[(0xD000, b"OTA"), (0x1004, b"xy")])
        b = img.read_bytes()
        self.assertEqual(b[0xD000:0xD003], b"OTA")
        self.assertEqual(b[0x1000:0x1008], b"BBBBxyBB")


if __name__ == "__main__":
    unittest.main()
