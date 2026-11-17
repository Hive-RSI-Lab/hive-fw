"""fw qemu: boot a built ESP-IDF image in Espressif's QEMU and judge the log.

    fw qemu [PROJECT] [--build DIR] [--expect REGEX] [--timeout S] [--settle S]
            [--psram 4M] [--log FILE] [--image-only]
            [--http 'METHOD /path [body]' ...] [--http-port N] [--hold S]
            [--scenario NAME|all|list ...]

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

Fault scenarios: fw.json "qemu": {"scenarios": {NAME: {"about", "overrides",
"steps"}}}, run with --scenario NAME (repeatable; 'all', 'list'). Each boots
afresh with its overrides (OVERRIDABLE) laid over the qemu block, then takes
its steps (parse_step): HTTP probes with retry, gdb `set` on the fault
shims' variables (qemu/adc.c, gpio.c, heap.c) over QEMU's gdb stub, waits,
console lines and log matches. Logs go to build/fw-qemu-NAME.log.
"""
from __future__ import annotations

import argparse
import csv
import http.client
import io
import json
import os
import re
import selectors
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootlog  # noqa: E402
import fwlib  # noqa: E402

SIZES = {"1MB": 1, "2MB": 2, "4MB": 4, "8MB": 8, "16MB": 16}
MACHINES = {"esp32": "esp32", "esp32s3": "esp32s3"}


def partitions(build: Path) -> dict[str, tuple[int, int, int, int]]:
    """name -> (type, subtype, offset, size) from the build's binary
    partition table (32-byte entries: magic 0x50AA, type, subtype, offset,
    size, name)."""
    t = (build / "partition_table" / "partition-table.bin").read_bytes()
    out = {}
    for i in range(0, len(t) - 31, 32):
        magic, typ, sub, off, size = struct.unpack_from("<HBBII", t, i)
        if magic != 0x50AA:
            break
        out[t[i + 12:i + 28].split(b"\0")[0].decode(errors="replace")] = (typ, sub, off, size)
    return out


def otadata(parts: dict, boot: str) -> list[tuple[int, bytes]]:
    """fw.json "qemu": {"boot": "ota_0"}: otadata that has the bootloader
    start that OTA app instead of the factory one (squeezelite's factory app
    is its recovery). One select entry, as IDF writes it: ota_seq N+1 for
    slot N, its CRC (crc32_le from all-ones, which is zlib's), state unset."""
    if boot not in parts:
        raise ValueError(f"qemu.boot: no partition {boot!r} (the table has {', '.join(parts)})")
    typ, sub = parts[boot][:2]
    if typ != 0 or not 0x10 <= sub < 0x20:
        raise ValueError(f"qemu.boot: {boot} isn't an OTA app partition")
    od = [p for p in parts.values() if p[:2] == (1, 0)]
    if not od:
        raise ValueError("qemu.boot: the partition table has no otadata")
    seq = sub - 0x10 + 1
    entry = struct.pack("<I20sII", seq, b"\xff" * 20, 0xFFFFFFFF, zlib.crc32(struct.pack("<I", seq), 0xFFFFFFFF))
    return [(od[0][2], entry)]


def nvs_csv(namespaces: dict) -> str:
    """fw.json qemu.nvs's {namespace: {key: value}} for one partition, as
    nvs_partition_gen.py's CSV: a string is a string, a whole number an i32."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["key", "type", "encoding", "value"])
    for ns, kv in namespaces.items():
        w.writerow([ns, "namespace", "", ""])
        for k, v in kv.items():
            if isinstance(v, bool) or not isinstance(v, (str, int)):
                raise ValueError(f"qemu.nvs {ns}.{k}: a string or a whole number, not {v!r}")
            w.writerow([k, "data", "string" if isinstance(v, str) else "i32", v])
    return buf.getvalue()


NVS_PAGE = 4096


NVS_ENTRIES = 126  # 32-byte entries a page


def nvs_free_entries(image: bytes) -> int:
    """Entries still blank in an NVS partition image's used pages (a page
    whose state word is all ones is blank, the one NVS keeps free). Each is
    room NVS's garbage collection would hand to a write."""
    free = 0
    for i in range(0, len(image), NVS_PAGE):
        if image[i:i + 4] == b"\xff\xff\xff\xff":
            continue
        bm = image[i + 32:i + 64]
        free += sum((bm[j // 4] >> (j % 4 * 2)) & 3 == 3 for j in range(NVS_ENTRIES))
    return free


def nvs_fill(namespaces: dict, ns: str, size: int, gen) -> dict:
    """namespaces with ns padded until the partition is full: every entry of
    every page but the one NVS keeps free is used, and none is erased, so
    garbage collection has nothing to reclaim and the next new key fails
    with ESP_ERR_NVS_NOT_ENOUGH_SPACE. gen(namespaces) is the partition
    image, or None when they don't fit. The padding is whole numbers, one
    entry each, so they pack each page to the last entry (a long string
    can't span pages, and the generator leaves the rest of a page blank);
    the count is found by bisection."""
    pages = size // NVS_PAGE
    if pages < 2:
        raise ValueError(f"qemu.nvs_fill: {size} bytes is under two NVS pages")
    base = {k: dict(v) for k, v in namespaces.items()}
    base.setdefault(ns, {})

    def with_fill(n: int) -> dict:
        out = {k: dict(v) for k, v in base.items()}
        out[ns].update({f"fw_fill_{i:05d}": i for i in range(n)})
        return out

    if gen(with_fill(0)) is None:
        raise ValueError(f"qemu.nvs_fill: the settings alone don't fit in {size} bytes")
    lo, hi = 0, pages * NVS_ENTRIES
    while lo < hi:
        mid = (lo + hi + 1) // 2
        lo, hi = (mid, hi) if gen(with_fill(mid)) is not None else (lo, mid - 1)
    return with_fill(lo)


def nvs_images(build: Path, pins: dict) -> list[tuple[int, bytes]]:
    """fw.json "qemu": {"nvs": {"partition": {"namespace": {"key": value}}}}:
    settings the firmware finds in flash on its first boot (squeezelite's
    eth_config, Wi-Fi credentials...), made with the IDF's own
    nvs_partition_gen.py and laid over the partition. Each boot starts from
    them: the image is rebuilt every run. "nvs_fill": {"partition":
    "namespace"} also pads that namespace until the partition is full
    (nvs_fill()), for a scenario that boots on a full flash."""
    q = pins.get("qemu", {})
    spec = dict(q.get("nvs") or {})
    fill = q.get("nvs_fill") or {}
    if not isinstance(fill, dict) or not all(isinstance(v, str) for v in fill.values()):
        raise ValueError('qemu.nvs_fill: {"partition": "namespace"}')
    for name in fill:
        spec.setdefault(name, {})
    if not spec:
        return []
    parts = partitions(build)
    root = fwlib.find_idf(pins["idf"])
    if not root:
        raise RuntimeError(f"qemu.nvs needs ESP-IDF {pins['idf']} (fw setup)")
    idf = root / f"esp-idf-{pins['idf']}"
    py = Path((idf / ".fw-ok").read_text(encoding="utf-8").strip()) / "bin" / "python"
    gen_py = idf / "components" / "nvs_flash" / "nvs_partition_generator" / "nvs_partition_gen.py"
    out = []
    for name, namespaces in spec.items():
        if name not in parts:
            raise RuntimeError(f"qemu.nvs: no partition {name!r} (the table has {', '.join(parts)})")
        off, size = parts[name][2:]
        c, b = build / f"fw-nvs-{name}.csv", build / f"fw-nvs-{name}.bin"

        def gen(nss: dict, check: bool = False) -> bytes | None:
            c.write_text(nvs_csv(nss), encoding="utf-8")
            r = subprocess.run([str(py), str(gen_py), "generate", str(c), str(b), hex(size)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                               env=dict(os.environ, PYTHONPATH=""))
            if r.returncode:
                if check:
                    raise RuntimeError(f"nvs_partition_gen for {name}: {r.stderr.strip()[-300:]}")
                return None
            return b.read_bytes()

        if name in fill:
            namespaces = nvs_fill(namespaces, fill[name], size, gen)
        out.append((off, gen(namespaces, check=True)))
    return out


def app_elf(build: Path, boot: str | None = None) -> Path | None:
    """The ELF of the app the bootloader starts: the binary flashed at the
    qemu.boot partition (or flasher_args' app), with .elf for .bin."""
    fa = json.loads((build / "flasher_args.json").read_text(encoding="utf-8"))
    name = (fa.get("app") or {}).get("file")
    if boot:
        off = partitions(build)[boot][2]
        name = next((f for o, f in fa["flash_files"].items() if int(o, 16) == off), name)
    elf = build / Path(name).with_suffix(".elf") if name else None
    return elf if elf and elf.is_file() else None


def merge(build: Path, out: Path, flash_size: str | None = None,
          extra: list[tuple[int, bytes]] | None = None) -> tuple[Path, str]:
    """One flash image from build/flasher_args.json, with extra (offset,
    data) laid on top; returns it and the chip."""
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
    for o, data in extra or []:
        if o + len(data) > len(img):
            raise ValueError(f"{len(data)} bytes at {o:#x} don't fit a {size} flash")
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


def parse_step(spec: dict | str) -> dict:
    """One scenario step (fw.json qemu.scenarios.NAME.steps), as a dict with
    its "kind":
      http: a probe (parse_probe), plus "retry": N to ask again once a
        second, up to N more times, until the status and expect match, and
        "status": "any" to take whatever comes back;
      set: {"set": "C expression"} or a list of them: gdb attaches over
        QEMU's gdb stub, runs `set var` on each and detaches (the fault
        shims' variables, or any global of the firmware's);
      wait: {"wait": S} seconds;
      send: {"send": "line", "expect": regex, "timeout": S}: the line to the
        console (UART0 input), and the regex the UART must show after it;
      log: {"log": regex, "timeout": S}: a line the UART must have shown
        since the step before this one began (so a fault step's effect, not
        a line from boot), or show within the timeout; "since": "boot"
        searches the whole UART log."""
    if isinstance(spec, str) or any(m in spec for m in ("get", "post", "put", "delete")):
        retry = spec.get("retry", 0) if isinstance(spec, dict) else 0
        anystatus = isinstance(spec, dict) and spec.get("status") == "any"
        if anystatus:
            spec = {k: v for k, v in spec.items() if k != "status"}
        p = parse_probe(spec)
        if isinstance(retry, bool) or not isinstance(retry, int) or retry < 0:
            raise ValueError(f"step {spec!r}: retry is a count")
        return dict(p, kind="http", retry=retry, status=None if anystatus else p["status"])
    if not isinstance(spec, dict):
        raise ValueError(f"step {spec!r}: want an object")
    timeout = spec.get("timeout", 10)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError(f"step {spec!r}: timeout is seconds")
    if "set" in spec:
        exprs = [spec["set"]] if isinstance(spec["set"], str) else spec["set"]
        if not exprs or not all(isinstance(e, str) and e.strip() and "\n" not in e for e in exprs):
            raise ValueError(f"step {spec!r}: set takes a C expression or a list of them")
        return {"kind": "set", "exprs": exprs}
    if "wait" in spec:
        w = spec["wait"]
        if isinstance(w, bool) or not isinstance(w, (int, float)) or w < 0:
            raise ValueError(f"step {spec!r}: wait is seconds")
        return {"kind": "wait", "secs": float(w)}
    if "send" in spec:
        if not isinstance(spec["send"], str) or any(ord(c) < 32 or c == "\x7f" for c in spec["send"]):
            raise ValueError(f"step {spec!r}: send takes one line, no control characters")
        if spec.get("expect"):
            re.compile(spec["expect"])
        return {"kind": "send", "line": spec["send"], "expect": spec.get("expect"), "timeout": float(timeout)}
    if "log" in spec:
        re.compile(spec["log"])
        since = spec.get("since", "step")
        if since not in ("step", "boot"):
            raise ValueError(f"step {spec!r}: since is \"step\" or \"boot\"")
        return {"kind": "log", "expect": spec["log"], "timeout": float(timeout), "since": since}
    raise ValueError(f"step {spec!r}: want get/post/put/delete, set, wait, send or log")


def describe(st: dict) -> str:
    k = st["kind"]
    return (f"{st['method']} {st['path']}" if k == "http" else
            "set " + "; ".join(st["exprs"]) if k == "set" else
            f"wait {st['secs']:g}s" if k == "wait" else
            f"send {st['line']!r}" if k == "send" else f"log /{st['expect']}/")


class Uart:
    """The UART so far, shared between the reading loop and the steps, and
    QEMU's stdin (UART0's input) for send steps."""

    def __init__(self, stdin=None):
        self.buf = bytearray()
        self.stdin = stdin

    def text(self, since: int = 0) -> str:
        return bytes(self.buf[since:]).decode(errors="replace")


class Steps(threading.Thread):
    """Runs the steps in order against the running guest, while the main loop
    keeps reading (and judging) the UART. The first HTTP step waits for any
    answer (DHCP and the server take a while after the expect line); after
    that each gets one try, or retry more. self.result: None while running,
    else a Verdict."""

    def __init__(self, port: int | None, steps: list[dict], deadline: float, uart: Uart | None = None,
                 gdb: tuple[str, Path, int] | None = None):
        super().__init__(daemon=True)
        self.port, self.steps, self.deadline, self.uart, self.gdb = port, steps, deadline, uart, gdb
        self.lines: list[str] = []
        self.result: bootlog.Verdict | None = None
        self.at = ""  # the step running now, as the failure would name it

    def run(self) -> None:
        try:
            self.result = self._run()
        except Exception as e:  # a step must end the run, not hang it
            self.result = bootlog.Verdict(False, f"step: {e}", True)

    def _run(self) -> bootlog.Verdict:
        answered = False
        self.prev_mark = self.mark = 0
        for st in self.steps:
            what = describe(st)
            self.at = ("http: " if st["kind"] == "http" else "") + what
            self.prev_mark, self.mark = self.mark, len(self.uart.buf) if self.uart else 0
            bad = getattr(self, "_" + st["kind"])(st, answered)
            answered |= st["kind"] == "http"
            if bad:
                return bootlog.Verdict(False, f"{'http: ' if st['kind'] == 'http' else ''}{what}: {bad}", True)
        n = len(self.steps)
        nhttp = sum(st["kind"] == "http" for st in self.steps)
        return bootlog.Verdict(True, (f"http: {n} request(s) answered as expected" if n == nhttp
                                      else f"{n} step(s) passed"), True)

    def _http(self, st: dict, answered: bool) -> str:
        tries = st["retry"] + 1
        while True:
            try:
                status, body = request(self.port, st, timeout=5 if not answered else 10)
            except (OSError, http.client.HTTPException) as e:
                if not answered:
                    if time.monotonic() > self.deadline:
                        self.lines.append(f"{describe(st)} -> no answer ({e})")
                        return f"no answer on port {self.port} ({e})"
                    time.sleep(1)
                    continue
                status, body = 0, f"({e})"
            answered = True
            tries -= 1
            bad = ("no answer: " + body if status == 0 and st["status"] is None
                   else f"status {status}, want {st['status']}" if st["status"] is not None and status != st["status"]
                   else f"reply doesn't match /{st['expect']}/" if st["expect"] and not re.search(st["expect"], body)
                   else "")
            if not bad or tries <= 0 or time.monotonic() > self.deadline:
                self.lines.append(f"{describe(st)} -> {status} {'FAIL: ' + bad if bad else 'ok'}\n"
                                  f"  {body.strip()[:2000]}")
                return f"{bad}: {body.strip()[:200]}" if bad else ""
            time.sleep(1)

    def _set(self, st: dict, answered: bool) -> str:
        if not self.gdb:
            return "no gdb (fw setup installs it) or no ELF for the booted app"
        exe, elf, port = self.gdb
        cmd = [exe, "-batch", "-nx", str(elf), "-ex", f"target remote 127.0.0.1:{port}"]
        for e in st["exprs"]:
            cmd += ["-ex", f"set var {e}"]
        cmd += ["-ex", "detach"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        errs = [ln for ln in r.stderr.splitlines() if ln.strip() and not ln.lower().startswith("warning")]
        self.lines.append(f"{describe(st)} -> {'ok' if not errs and r.returncode == 0 else 'FAIL'}"
                          + "".join(f"\n  {ln}" for ln in errs))
        return f"gdb: {errs[0] if errs else f'exit {r.returncode}'}" if errs or r.returncode else ""

    def _wait(self, st: dict, answered: bool) -> str:
        time.sleep(st["secs"])
        self.lines.append(describe(st))
        return ""

    def _until(self, pattern: str, since: int, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while True:
            if re.search(pattern, self.uart.text(since)):
                return True
            if time.monotonic() > end:
                return False
            time.sleep(0.2)

    def _send(self, st: dict, answered: bool) -> str:
        mark = len(self.uart.buf)
        self.uart.stdin.write(st["line"].encode() + b"\n")
        self.uart.stdin.flush()
        ok = not st["expect"] or self._until(st["expect"], mark, st["timeout"])
        self.lines.append(f"{describe(st)} -> {'ok' if ok else 'FAIL'}\n  {self.uart.text(mark).strip()[-2000:]}")
        return "" if ok else f"the console didn't show /{st['expect']}/ within {st['timeout']:g}s"

    def _log(self, st: dict, answered: bool) -> str:
        ok = self._until(st["expect"], 0 if st.get("since") == "boot" else self.prev_mark, st["timeout"])
        self.lines.append(f"{describe(st)} -> {'ok' if ok else 'FAIL'}")
        return "" if ok else f"not in the UART within {st['timeout']:g}s"


def run(qemu: Path, image: Path, chip: str, expect: str, timeout: float, settle: float,
        psram: str | None, log_path: Path | None, http_probes: list[dict] | None = None,
        guest_port: int = 80, host_port: int | None = None, hold: float = 0,
        gdb: tuple[str, Path] | None = None) -> bootlog.Verdict:
    """Boot and judge. With http_probes (parse_probe's or parse_step's) or
    hold, the guest's guest_port is forwarded to 127.0.0.1:host_port over
    QEMU's open_eth (user networking: the guest gets 10.0.2.15 by DHCP), and
    the steps run once the expect line shows. hold keeps a passing run up
    that many more seconds, for requests of your own. gdb is (gdb, the
    booted app's ELF), for set steps: QEMU's gdb stub listens on a free
    port of 127.0.0.1."""
    if chip not in MACHINES:
        return bootlog.Verdict(False, f"no QEMU machine for {chip} (esp32, esp32s3 only)", False)
    steps = [st if "kind" in st else dict(st, kind="http", retry=0) for st in http_probes or []]
    nic = "user,model=open_eth"
    if any(st["kind"] == "http" for st in steps) or hold:
        host_port = host_port or free_port()
        nic += f",hostfwd=tcp:127.0.0.1:{host_port}-:{guest_port}"
    cmd = [str(qemu), "-nographic", "-no-reboot", "-machine", MACHINES[chip],
           "-drive", f"file={image},if=mtd,format=raw",
           "-global", f"driver=timer.{chip}.timg,property=wdt_disable,value=true",
           "-nic", nic]
    if psram:
        cmd += ["-m", psram]
    gdb_at = None
    if gdb and any(st["kind"] == "set" for st in steps):
        gdb_at = (str(gdb[0]), gdb[1], free_port())
        cmd += ["-gdb", f"tcp:127.0.0.1:{gdb_at[2]}"]
    sends = any(st["kind"] == "send" for st in steps)
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE if sends else subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, env=fwlib.qemu_env(qemu))
    sel = selectors.DefaultSelector()
    sel.register(p.stdout, selectors.EVENT_READ)
    uart = Uart(p.stdin)
    buf = uart.buf
    start = time.monotonic()
    reached_at = None
    prober: Steps | None = None
    held = False
    v = bootlog.Verdict(False, "timed out", False)
    try:
        while True:
            now = time.monotonic()
            if now - start > timeout and not held:
                v = bootlog.judge(buf.decode(errors="replace"), expect)
                if not v.ok:
                    v = bootlog.Verdict(False, f"timed out after {timeout:.0f}s: {v.reason}", False)
                elif prober and prober.result is not None and not prober.result.ok:
                    v = prober.result
                elif prober and prober.result is None:
                    v = bootlog.Verdict(False, f"timed out after {timeout:.0f}s waiting on the steps"
                                                         + (f" ({prober.at})" if prober.at else ""), True)
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
                buf.extend(chunk)
                v = bootlog.judge(buf.decode(errors="replace"), expect, done=False)
                if not v.ok and v.reason != "still booting":
                    break
                if v.ok and reached_at is None:
                    reached_at = time.monotonic()
                    if steps:
                        prober = Steps(host_port, steps, start + timeout, uart, gdb_at)
                        prober.start()
    finally:
        p.kill()
        p.wait()
        if log_path:
            extra = b""
            if prober and prober.lines:
                extra = ("\n--- fw qemu: HTTP over open_eth ---\n" + "\n".join(prober.lines) + "\n").encode()
            log_path.write_bytes(bytes(buf) + extra)
    if reached_at is not None and v.ok:
        v = bootlog.judge(buf.decode(errors="replace"), expect)
        if v.ok and prober and prober.result:
            v = (bootlog.Verdict(True, f"{v.reason}; {prober.result.reason}", True) if prober.result.ok
                 else prober.result)
    return v


# What a scenario's "overrides" may change: what the flash image and the
# boot are made of. The ADC, pins and Wi-Fi are linked into the QEMU build,
# so a scenario changes those with set steps instead.
OVERRIDABLE = {"nvs", "nvs_fill", "boot", "psram", "flash", "expect", "timeout", "settle"}


def scenarios(q: dict, names: list[str]) -> list[tuple[str, dict, list[dict]]]:
    """fw.json qemu.scenarios picked by name ("all" for every one), each as
    (name, the qemu block with its overrides, its parsed steps)."""
    have = q.get("scenarios") or {}
    if not have:
        raise ValueError("fw.json qemu has no scenarios")
    if "all" in names:
        names = list(have)
    out = []
    for n in names:
        if n not in have:
            raise ValueError(f"no scenario {n!r} (fw.json has {', '.join(have)})")
        sc = have[n]
        ov = sc.get("overrides") or {}
        bad = set(ov) - OVERRIDABLE
        if bad:
            raise ValueError(f"scenario {n}: overrides can't change {', '.join(sorted(bad))}"
                             f" (only {', '.join(sorted(OVERRIDABLE))}; the rest is in the build)")
        if not sc.get("steps"):
            raise ValueError(f"scenario {n}: no steps")
        try:
            steps = [parse_step(x) for x in sc["steps"]]
        except (ValueError, re.error) as e:
            raise ValueError(f"scenario {n}: {e}") from None
        out.append((n, {**q, **ov}, steps))
    return out


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
    ap.add_argument("--scenario", action="append", default=[], metavar="NAME",
                    help="run fw.json qemu.scenarios NAME (repeat it; 'all' for each; 'list' to list them)")
    ap.add_argument("--image-only", action="store_true", help="write build/fw-flash.bin and stop")
    a = ap.parse_args(argv)
    pins = fwlib.read_pins(a.project)
    q = pins.get("qemu", {})
    if "list" in a.scenario:
        for n, sc in (q.get("scenarios") or {}).items():
            print(f"{n}: {sc.get('about', '')}")
        return 0
    build = a.build or fwlib.qemu_build_dir(fwlib.src_dir(a.project.resolve(), pins), pins)
    if not (build / "flasher_args.json").is_file():
        print(f"fw qemu: no {build}/flasher_args.json: build first (fw build"
              + (" --qemu" if build.name == "build-qemu" else "") + ")", file=sys.stderr)
        return 2
    if a.scenario and (a.log or a.http or a.hold):
        print("fw qemu: --scenario takes its steps and log from fw.json: drop --log, --http and --hold",
              file=sys.stderr)
        return 2
    try:
        if a.scenario:
            runs = scenarios(q, a.scenario)
            if a.image_only and len(runs) > 1:
                raise ValueError("--image-only writes one image: name one scenario")
        else:
            runs = [(None, q, [parse_probe(x) for x in (a.http or q.get("http") or [])])]
    except (ValueError, re.error) as e:
        print(f"fw qemu: {e}", file=sys.stderr)
        return 2
    qemu = None if a.image_only else fwlib.find_qemu()
    if not qemu and not a.image_only:
        print("fw qemu: no QEMU: run fw setup", file=sys.stderr)
        return 2
    failed = []
    for name, rq, steps in runs:
        try:
            image, chip = merge(build, build / "fw-flash.bin", a.flash_size or rq.get("flash"),
                                nvs_images(build, {**pins, "qemu": rq})
                                + (otadata(partitions(build), rq["boot"]) if rq.get("boot") else []))
        except (RuntimeError, ValueError, subprocess.CalledProcessError) as e:
            print(f"fw qemu: {name + ': ' if name else ''}{e}", file=sys.stderr)
            if not name:
                return 2
            failed.append(name)
            continue
        if a.image_only:
            print(image)
            return 0
        expect = a.expect or rq.get("expect") or bootlog.DEFAULT_EXPECT
        try:
            re.compile(expect)
        except re.error as e:
            print(f"fw qemu: {name + ': ' if name else ''}expect /{expect}/: {e}", file=sys.stderr)
            return 2
        gdb = None
        if any(st.get("kind") == "set" for st in steps):
            exe, elf = fwlib.find_gdb(chip), app_elf(build, rq.get("boot"))
            if not exe or not elf:
                print(f"fw qemu: scenario {name} sets variables and needs "
                      + ("gdb (fw setup)" if not exe else f"the app's ELF in {build}"), file=sys.stderr)
                return 2
            gdb = (str(exe), elf)
        log = (a.log or build / "fw-qemu.log") if not name else build / f"fw-qemu-{name}.log"
        v = run(qemu, image, chip, expect, a.timeout or rq.get("timeout", 30),
                a.settle if a.settle is not None else rq.get("settle", 3),
                a.psram or rq.get("psram"), log, steps, int(rq.get("http_port", 80)), a.http_port, a.hold,
                gdb=gdb)
        if a.http and log.is_file():  # asked by hand: show the replies
            t = log.read_text(errors="replace")
            i = t.find("--- fw qemu: HTTP")
            if i >= 0:
                print(t[i:].rstrip())
        print(f"fw qemu: {'scenario ' + name + ': ' if name else ''}{v}  (log: {log})", flush=True)
        if not v.ok:
            failed.append(name)
    if len(runs) > 1:
        print(f"fw qemu: {len(runs) - len(failed)}/{len(runs)} scenarios passed"
              + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
