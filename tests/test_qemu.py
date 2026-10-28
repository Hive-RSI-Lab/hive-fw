import json
import tempfile
import unittest
from pathlib import Path

from tests import _util  # noqa: F401
import qemu


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


if __name__ == "__main__":
    unittest.main()
