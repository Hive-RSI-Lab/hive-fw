"""fw qemu: boot a built ESP-IDF image in Espressif's QEMU and judge the log.

    fw qemu [PROJECT] [--build DIR] [--expect REGEX] [--timeout S] [--settle S]
            [--psram 4M] [--log FILE] [--image-only]
            [--http 'METHOD /path [body]' ...] [--http-port N] [--hold S]

Reads the build's flasher_args.json, lays every binary at its offset in one
flash image (merge(): no esptool needed), boots it on the `esp32` machine
(-no-reboot, so a panic or reset ends the run), and watches the UART. It
passes when the expect pattern shows and nothing fatal follows for --settle
seconds; it fails at the first fatal line (bootlog.FATAL) or at --timeout.
Defaults come from the project's fw.json "qemu" block. There's no Wi-Fi or
I2S codec in QEMU: firmware that waits on them should still reach app_main
and stay up; give --expect a line your own code logs to go further.

QEMU does emulate an Ethernet MAC (open_eth), on user networking. With
fw.json "qemu": {"http": [{"get": "/api/v1/info", "expect": "regex"}, ...]}
(or --http 'GET /path', 'POST /path {json}'), port 80 of the guest
("http_port") is forwarded to a free port on 127.0.0.1, and once the expect
line shows, each request is made in order: the run fails on a wrong status
(default 200) or a reply that doesn't match. Requests and replies are added
to the log. --hold S keeps a passing run up for requests of your own.
Firmware that brings Ethernet up through the ESP32 EMAC gets open_eth from
"eth": "emac" (fw build --qemu; qemu/openeth.c).
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import selectors
import socket
import subprocess
import sys
import threading
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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def parse_probe(spec: dict | str) -> dict:
    """One HTTP probe: {"get": "/path"} or {"post": "/path", "json": {...}}
    (or "body": "text"), with optional "status" (default 200) and "expect"
    (a regex the reply body must match). A string is the CLI's form:
    "GET /path" or "POST /path {json}"."""
    if isinstance(spec, str):
        parts = spec.split(None, 2)
        if len(parts) < 2:
            raise ValueError(f"http probe {spec!r}: want 'METHOD /path [body]'")
        spec = {parts[0].lower(): parts[1]}
        if len(parts) == 3:
            spec["body"] = parts[2]
    methods = [m for m in ("get", "post", "put", "delete") if m in spec]
    if len(methods) != 1 or not str(spec[methods[0]]).startswith("/"):
        raise ValueError(f"http probe {spec!r}: want one of get/post/put/delete with a /path")
    body = spec.get("body")
    if "json" in spec:
        body = json.dumps(spec["json"])
    if spec.get("expect"):
        re.compile(spec["expect"])
    return {"method": methods[0].upper(), "path": spec[methods[0]],
            "body": body.encode() if isinstance(body, str) else None,
            "status": int(spec.get("status", 200)), "expect": spec.get("expect")}


def request(port: int, p: dict, timeout: float = 10) -> tuple[int, str]:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        hdrs = {"Content-Type": "application/json"} if p["body"] is not None else {}
        c.request(p["method"], p["path"], body=p["body"], headers=hdrs)
        r = c.getresponse()
        return r.status, r.read().decode(errors="replace")
    finally:
        c.close()


class Prober(threading.Thread):
    """Runs the probes in order against the forwarded port, while the main
    loop keeps reading (and judging) the UART. It waits for the first to get
    any answer (DHCP and the server take a while after the expect line),
    then gives each one try. self.result: None while running, else a Verdict."""

    def __init__(self, port: int, probes: list[dict], deadline: float):
        super().__init__(daemon=True)
        self.port, self.probes, self.deadline = port, probes, deadline
        self.lines: list[str] = []
        self.result: bootlog.Verdict | None = None

    def run(self) -> None:
        try:
            self.result = self._run()
        except Exception as e:  # a probe must end the run, not hang it
            self.result = bootlog.Verdict(False, f"http: {e}", True)

    def _run(self) -> bootlog.Verdict:
        first = self.probes[0]
        while True:
            try:
                got = request(self.port, first, timeout=5)
                break
            except (OSError, http.client.HTTPException) as e:
                if time.monotonic() > self.deadline:
                    return bootlog.Verdict(False, f"http: no answer on port {self.port} ({e})", True)
                time.sleep(1)
        for i, p in enumerate(self.probes):
            if i:
                try:
                    got = request(self.port, p)
                except (OSError, http.client.HTTPException) as e:
                    got = (0, f"({e})")
            status, body = got
            what = f"{p['method']} {p['path']}"
            bad = (f"status {status}, want {p['status']}" if status != p["status"] else
                   f"reply doesn't match /{p['expect']}/" if p["expect"] and not re.search(p["expect"], body) else "")
            self.lines.append(f"{what} -> {status} {'FAIL: ' + bad if bad else 'ok'}\n  {body.strip()[:2000]}")
            if bad:
                return bootlog.Verdict(False, f"http: {what}: {bad}: {body.strip()[:200]}", True)
        return bootlog.Verdict(True, f"http: {len(self.probes)} request(s) answered as expected", True)


def run(qemu: Path, image: Path, chip: str, expect: str, timeout: float, settle: float,
        psram: str | None, log_path: Path | None, http_probes: list[dict] | None = None,
        guest_port: int = 80, host_port: int | None = None, hold: float = 0) -> bootlog.Verdict:
    """Boot and judge. With http_probes (parse_probe's) or hold, the guest's
    guest_port is forwarded to 127.0.0.1:host_port over QEMU's open_eth
    (user networking: the guest gets 10.0.2.15 by DHCP), and the probes run
    once the expect line shows. hold keeps a passing run up that many more
    seconds, for requests of your own."""
    if chip not in MACHINES:
        return bootlog.Verdict(False, f"no QEMU machine for {chip} (esp32, esp32s3 only)", False)
    nic = "user,model=open_eth"
    if http_probes or hold:
        host_port = host_port or free_port()
        nic += f",hostfwd=tcp:127.0.0.1:{host_port}-:{guest_port}"
    cmd = [str(qemu), "-nographic", "-no-reboot", "-machine", MACHINES[chip],
           "-drive", f"file={image},if=mtd,format=raw",
           "-global", f"driver=timer.{chip}.timg,property=wdt_disable,value=true",
           "-nic", nic]
    if psram:
        cmd += ["-m", psram]
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, env=fwlib.qemu_env(qemu))
    sel = selectors.DefaultSelector()
    sel.register(p.stdout, selectors.EVENT_READ)
    buf = b""
    start = time.monotonic()
    reached_at = None
    prober: Prober | None = None
    held = False
    v = bootlog.Verdict(False, "timed out", False)
    try:
        while True:
            now = time.monotonic()
            if now - start > timeout and not held:
                v = bootlog.judge(buf.decode(errors="replace"), expect)
                if not v.ok:
                    v = bootlog.Verdict(False, f"timed out after {timeout:.0f}s: {v.reason}", False)
                elif prober and prober.result is None:
                    v = bootlog.Verdict(False, f"timed out after {timeout:.0f}s waiting on the HTTP requests", True)
                break
            if prober and prober.result is not None and not prober.result.ok:
                v = prober.result
                break
            if reached_at is not None and now - reached_at >= settle and (prober is None or prober.result):
                if hold and not held:
                    held, reached_at = True, now + hold - settle
                    print(f"fw qemu: up; http://127.0.0.1:{host_port}/ reaches the firmware's port "
                          f"{guest_port} for {hold:.0f}s", flush=True)
                    continue
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
                    if http_probes:
                        prober = Prober(host_port, http_probes, start + timeout)
                        prober.start()
    finally:
        p.kill()
        p.wait()
        if log_path:
            extra = b""
            if prober and prober.lines:
                extra = ("\n--- fw qemu: HTTP over open_eth ---\n" + "\n".join(prober.lines) + "\n").encode()
            log_path.write_bytes(buf + extra)
    if reached_at is not None and v.ok:
        v = bootlog.judge(buf.decode(errors="replace"), expect)
        if v.ok and prober and prober.result:
            v = bootlog.Verdict(True, f"{v.reason}; {prober.result.reason}", True)
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
    ap.add_argument("--http", action="append", metavar="'METHOD /path [body]'",
                    help="a request to make once booted, in place of fw.json's qemu.http (repeat it)")
    ap.add_argument("--http-port", type=int, help="the host port to forward (default: a free one)")
    ap.add_argument("--hold", type=float, default=0, metavar="S",
                    help="keep a passing run up S more seconds, its port forwarded, for requests of your own")
    ap.add_argument("--image-only", action="store_true", help="write build/fw-flash.bin and stop")
    a = ap.parse_args(argv)
    pins = fwlib.read_pins(a.project)
    q = pins.get("qemu", {})
    build = a.build or fwlib.qemu_build_dir(fwlib.src_dir(a.project.resolve(), pins), pins)
    if not (build / "flasher_args.json").is_file():
        print(f"fw qemu: no {build}/flasher_args.json: build first (fw build"
              + (" --qemu" if build.name == "build-qemu" else "") + ")", file=sys.stderr)
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
    try:
        probes = [parse_probe(x) for x in (a.http or q.get("http") or [])]
    except (ValueError, re.error) as e:
        print(f"fw qemu: {e}", file=sys.stderr)
        return 2
    log = a.log or build / "fw-qemu.log"
    v = run(qemu, image, chip, expect, a.timeout or q.get("timeout", 30),
            a.settle if a.settle is not None else q.get("settle", 3),
            a.psram or q.get("psram"), log, probes, int(q.get("http_port", 80)), a.http_port, a.hold)
    if a.http and log.is_file():  # asked by hand: show the replies
        t = log.read_text(errors="replace")
        i = t.find("--- fw qemu: HTTP")
        if i >= 0:
            print(t[i:].rstrip())
    print(f"fw qemu: {v}  (log: {log})")
    return 0 if v.ok else 1


if __name__ == "__main__":
    sys.exit(main())
