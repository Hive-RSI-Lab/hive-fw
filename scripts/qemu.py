"""fw qemu: boot a built ESP-IDF image in Espressif's QEMU and judge the log.

    fw qemu [PROJECT] [--build DIR] [--expect REGEX] [--timeout S] [--settle S]
            [--psram 4M] [--log FILE] [--image-only]

Reads the build's flasher_args.json, lays every binary at its offset in one
flash image (merge(): no esptool needed), boots it on the `esp32` machine
(-no-reboot, so a panic or reset ends the run), and watches the UART. It
passes when the expect pattern shows and nothing fatal follows for --settle
seconds; it fails at the first fatal line (bootlog.FATAL) or at --timeout.
Defaults come from the project's fw.json "qemu" block. There's no Wi-Fi or
I2S codec in QEMU: firmware that waits on them should still reach app_main
and stay up; give --expect a line your own code logs to go further.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootlog  # noqa: E402
import fwlib  # noqa: E402

SIZES = {"1MB": 1, "2MB": 2, "4MB": 4, "8MB": 8, "16MB": 16}
MACHINES = {"esp32": "esp32", "esp32s3": "esp32s3"}


def merge(build: Path, out: Path, flash_size: str | None = None) -> tuple[Path, str]:
    """One flash image from build/flasher_args.json; returns it and the chip."""
    fa = json.loads((build / "flasher_args.json").read_text(encoding="utf-8"))
    size = flash_size or fa.get("flash_settings", {}).get("flash_size", "4MB")
    if size not in SIZES:
        size = "4MB"  # "detect"/"keep": QEMU wants a real size; 4MB is WROVER's and most modules'
    img = bytearray(b"\xff" * (SIZES[size] << 20))
    for off, name in fa["flash_files"].items():
        data = (build / name).read_bytes()
        o = int(off, 16)
        if o + len(data) > len(img):
            raise ValueError(f"{name} at {off} doesn't fit a {size} flash")
        img[o:o + len(data)] = data
    out.write_bytes(bytes(img))
    chip = fa.get("extra_esptool_args", {}).get("chip", "esp32")
    return out, chip


def run(qemu: Path, image: Path, chip: str, expect: str, timeout: float, settle: float,
        psram: str | None, log_path: Path | None) -> bootlog.Verdict:
    if chip not in MACHINES:
        return bootlog.Verdict(False, f"no QEMU machine for {chip} (esp32, esp32s3 only)", False)
    cmd = [str(qemu), "-nographic", "-no-reboot", "-machine", MACHINES[chip],
           "-drive", f"file={image},if=mtd,format=raw",
           "-global", f"driver=timer.{chip}.timg,property=wdt_disable,value=true",
           "-nic", "user,model=open_eth"]
    if psram:
        cmd += ["-m", psram]
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, env=fwlib.qemu_env(qemu))
    sel = selectors.DefaultSelector()
    sel.register(p.stdout, selectors.EVENT_READ)
    buf = b""
    start = time.monotonic()
    reached_at = None
    v = bootlog.Verdict(False, "timed out", False)
    try:
        while True:
            now = time.monotonic()
            if now - start > timeout:
                v = bootlog.judge(buf.decode(errors="replace"), expect)
                if not v.ok:
                    v = bootlog.Verdict(False, f"timed out after {timeout:.0f}s: {v.reason}", False)
                break
            if reached_at is not None and now - reached_at >= settle:
                break
            if sel.select(timeout=0.2):
                chunk = os.read(p.stdout.fileno(), 65536)
                if not chunk:  # QEMU exited: -no-reboot turns a reset into an exit
                    v = bootlog.judge(buf.decode(errors="replace"), expect)
                    if v.ok:
                        v = bootlog.Verdict(False, "QEMU exited (the chip reset)", True)
                    break
                buf += chunk
                v = bootlog.judge(buf.decode(errors="replace"), expect, done=False)
                if not v.ok and v.reason != "still booting":
                    break
                if v.ok and reached_at is None:
                    reached_at = time.monotonic()
    finally:
        p.kill()
        p.wait()
        if log_path:
            log_path.write_bytes(buf)
    if reached_at is not None and v.ok:
        v = bootlog.judge(buf.decode(errors="replace"), expect)
    return v


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fw qemu", description=__doc__.split("\n")[0])
    ap.add_argument("project", nargs="?", type=Path, default=Path.cwd())
    ap.add_argument("--build", type=Path)
    ap.add_argument("--expect")
    ap.add_argument("--timeout", type=float)
    ap.add_argument("--settle", type=float)
    ap.add_argument("--psram")
    ap.add_argument("--flash-size")
    ap.add_argument("--log", type=Path)
    ap.add_argument("--image-only", action="store_true", help="write build/fw-flash.bin and stop")
    a = ap.parse_args(argv)
    pins = fwlib.read_pins(a.project)
    q = pins.get("qemu", {})
    build = a.build or a.project / "build"
    if not (build / "flasher_args.json").is_file():
        print(f"fw qemu: no {build}/flasher_args.json: build first (fw build)", file=sys.stderr)
        return 2
    image, chip = merge(build, build / "fw-flash.bin", a.flash_size or q.get("flash"))
    if a.image_only:
        print(image)
        return 0
    qemu = fwlib.find_qemu()
    if not qemu:
        print("fw qemu: no QEMU: run fw setup", file=sys.stderr)
        return 2
    expect = a.expect or q.get("expect") or bootlog.DEFAULT_EXPECT
    re.compile(expect)
    v = run(qemu, image, chip, expect, a.timeout or q.get("timeout", 30),
            a.settle if a.settle is not None else q.get("settle", 3),
            a.psram or q.get("psram"), a.log or build / "fw-qemu.log")
    print(f"fw qemu: {v}  (log: {a.log or build / 'fw-qemu.log'})")
    return 0 if v.ok else 1


if __name__ == "__main__":
    sys.exit(main())
