"""Judge an ESP32 serial log: did it boot, and did it stay up?

Used by `fw qemu` (a QEMU boot) and `fw monitor` (a real board). Pure text in,
verdict out, so it's tested without a toolchain.

    python3 bootlog.py LOG [--expect REGEX]   exit 0 on a good boot, 1 not
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass

# Reached app_main: IDF 5+ says "main_task: Calling app_main()"; IDF 4.x
# logs "cpu_start: Starting scheduler" just before it.
DEFAULT_EXPECT = r"main_task: Calling app_main\(\)|cpu_start: Starting scheduler"

# Each of these means the firmware died or is about to reboot.
FATAL = [
    (r"Guru Meditation Error", "panic (Guru Meditation)"),
    (r"abort\(\) was called", "abort()"),
    (r"assert(ion)? failed|assert failed:", "assert failed"),
    (r"Stack canary watchpoint triggered|stack overflow", "stack overflow"),
    (r"Task watchdog got triggered|task_wdt: Task watchdog", "task watchdog"),
    (r"Brownout detector was triggered", "brownout"),
    (r"ESP_ERROR_CHECK failed", "ESP_ERROR_CHECK failed"),
    (r"Rebooting\.\.\.", "reboot"),
    (r"invalid header: 0x[0-9a-f]+", "no valid image in flash"),
    (r"Failed to verify app image|No bootable app partitions", "bootloader found no app"),
    (r"CORRUPT HEAP|heap_caps_alloc.*failed", "heap error"),
]
_ROM_BOOT = re.compile(r"^rst:0x[0-9a-f]+ \(([A-Z_0-9]+)\)", re.M)


@dataclass
class Verdict:
    ok: bool
    reason: str
    reached: bool  # the expect pattern was seen

    def __str__(self) -> str:
        return ("ok: " if self.ok else "FAIL: ") + self.reason


def judge(log: str, expect: str = DEFAULT_EXPECT, done: bool = True) -> Verdict:
    """The verdict on a log so far. With done=False it's a running check:
    a fatal line fails at once, otherwise it isn't ok until the log ends."""
    for pat, what in FATAL:
        m = re.search(pat, log)
        if m:
            line = log[log.rfind("\n", 0, m.start()) + 1:].split("\n", 1)[0].strip()
            return Verdict(False, f"{what}: {line}", bool(re.search(expect, log[:m.start()])))
    boots = _ROM_BOOT.findall(log)
    if len(boots) > 1:
        return Verdict(False, f"rebooted {len(boots) - 1} time(s): {', '.join(boots[1:])}", False)
    m = re.search(expect, log)
    if m:
        return Verdict(True, f"reached /{expect}/ and stayed up", True)
    if not log.strip():
        return Verdict(False, "no serial output", False)
    return Verdict(False, f"never reached /{expect}/" if done else "still booting", False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bootlog")
    ap.add_argument("log")
    ap.add_argument("--expect", default=DEFAULT_EXPECT)
    a = ap.parse_args(argv)
    with open(a.log, encoding="utf-8", errors="replace") as f:
        v = judge(f.read(), a.expect)
    print(v)
    return 0 if v.ok else 1


if __name__ == "__main__":
    sys.exit(main())
