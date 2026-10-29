"""fw's ESP-IDF commands: run idf.py and its tools with a project's pinned IDF.

    fw env [PROJECT]                 print the exports (eval "$(fw env)")
    fw idf [PROJECT] -- ARGS...      idf.py ARGS in the project, its IDF's env set
    fw build [PROJECT] [ARGS...]     idf.py set-target (first time) + build
    fw flash [PROJECT] --port P      idf.py -p P flash (P: /dev/ttyUSB0, rfc2217://host:4000, ...)
    fw monitor [PROJECT] --port P [--seconds S] [--expect RE]
                                     read the UART S seconds (no tty needed), judge it like fw qemu
    fw ports                         the serial ports this sandbox can see, and $FW_PORT
    fw doctor [PROJECT]              what's installed and what the project pins

PROJECT defaults to the current directory. A port can be a network one:
`rfc2217://host:port` from esptool's esp_rfc2217_server.py run beside the
board, so an agent flashes a board on another machine with no USB here.
"""
from __future__ import annotations

import glob
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootlog  # noqa: E402
import fwlib  # noqa: E402


def die(msg: str, code: int = 2) -> int:
    print(f"fw: {msg}", file=sys.stderr)
    return code


def split_project(args: list[str]) -> tuple[Path, list[str]]:
    """The leading arg is the project when it's an existing directory."""
    if args and not args[0].startswith("-") and Path(args[0]).is_dir():
        return Path(args[0]).resolve(), args[1:]
    return Path.cwd(), args


def env_for(project: Path) -> tuple[dict[str, str], dict] | None:
    pins = fwlib.read_pins(project)
    root = fwlib.find_idf(pins["idf"])
    if not root:
        return None
    return fwlib.idf_env(root, pins["idf"], pins.get("python")), pins


def need_env(project: Path):
    got = env_for(project)
    if not got:
        pins = fwlib.read_pins(project)
        raise SystemExit(die(f"ESP-IDF {pins['idf']} isn't installed: fw setup --project {project} "
                             f"(or ask a warden for it in /hive/tools/fw)"))
    os.nice(int(os.environ.get("FW_NICE") or 10))  # builds share the machine: below interactive work
    return got


def idf_py(env: dict, project: Path, args: list[str]) -> int:
    cmd = [os.path.join(env["IDF_PYTHON_ENV_PATH"], "bin", "python"),
           os.path.join(env["IDF_PATH"], "tools", "idf.py"), "-C", str(project)] + args
    print("fw: " + shlex.join(["idf.py"] + args), file=sys.stderr, flush=True)
    os.execve(cmd[0], cmd, env)


def cmd_env(args: list[str]) -> int:
    project, _ = split_project(args)
    env, pins = need_env(project)
    for k in ("IDF_PATH", "IDF_TOOLS_PATH", "IDF_PYTHON_ENV_PATH", "PATH", "OPENOCD_SCRIPTS", "ESP_ROM_ELF_DIR"):
        if env.get(k):
            print(f"export {k}={shlex.quote(env[k])}")
    q = fwlib.find_qemu()
    if q:
        print(f"export FW_QEMU={shlex.quote(str(q))}")
    return 0


def cmd_idf(args: list[str]) -> int:
    project, rest = split_project(args)
    if rest[:1] == ["--"]:
        rest = rest[1:]
    env, pins = need_env(project)
    return idf_py(env, fwlib.src_dir(project, pins), rest)


def prepare(project: Path, pins: dict, env: dict) -> Path:
    """Run the project's "prepare" step, if it has one; the IDF project dir."""
    src = fwlib.src_dir(project, pins)
    if pins.get("prepare"):
        cmd = fwlib.expand(pins["prepare"], project, src)
        print(f"fw: prepare: {cmd}", file=sys.stderr, flush=True)
        r = subprocess.run(["sh", "-c", cmd], cwd=project, env=env, stdout=sys.stderr)
        if r.returncode:
            raise SystemExit(die(f"prepare failed ({r.returncode}): {cmd}", r.returncode))
    return src


def run_idf(env: dict, src: Path, args: list[str]) -> int:
    print("fw: " + shlex.join(["idf.py"] + args), file=sys.stderr, flush=True)
    return subprocess.run([os.path.join(env["IDF_PYTHON_ENV_PATH"], "bin", "python"),
                           os.path.join(env["IDF_PATH"], "tools", "idf.py"), "-C", str(src)] + args,
                          env=env).returncode


def sdkconfig_key(line: str) -> str | None:
    """CONFIG_X for "CONFIG_X=v" and "# CONFIG_X is not set"; None for other lines."""
    line = line.strip()
    if line.startswith("CONFIG_") and "=" in line:
        return line.split("=", 1)[0]
    if line.startswith("# CONFIG_") and line.endswith(" is not set"):
        return line[2:-len(" is not set")]
    return None


def sdkconfig_renames(*roots: Path) -> dict[str, str]:
    """Deprecated option -> its current name, from every sdkconfig.rename
    under the roots (IDF's components and the project's). "!CONFIG_X" (an
    inverted rename, IDF 5) counts as the same option."""
    out: dict[str, str] = {}
    for root in roots:
        files = [root / "sdkconfig.rename"] + sorted(root.glob("components/*/sdkconfig.rename"))
        for f in files:
            if not f.is_file():
                continue
            for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = ln.split("#", 1)[0].split()
                if len(parts) == 2:
                    out[parts[0]] = parts[1].lstrip("!")
    return out


def overlay_sdkconfig(base: str, overlay: list[str], renames: dict[str, str] | None = None) -> str:
    """base with every option the overlay names dropped, its deprecated
    aliases too (renames: old -> new, else a stale alias such as
    CONFIG_FLASHMODE_QIO=y would put the board's value back), then the overlay."""
    renames = renames or {}
    canon = lambda k: renames.get(k, k)  # noqa: E731
    names = {canon(sdkconfig_key(ln)) for ln in overlay if sdkconfig_key(ln)}
    kept = [ln for ln in base.splitlines()
            if sdkconfig_key(ln) is None or canon(sdkconfig_key(ln)) not in names]
    return "\n".join(kept + list(overlay)) + "\n"


def cmd_build(args: list[str]) -> int:
    """fw build [P] [--qemu] [--no-prepare] [idf.py args]: --qemu builds the
    QEMU variant (fw.json qemu.sdkconfig lines on top of the project's
    sdkconfig) into build-qemu/, leaving build/ for the board."""
    project, rest = split_project(args)
    qemu = "--qemu" in rest
    skip_prep = "--no-prepare" in rest
    rest = [a for a in rest if a not in ("--qemu", "--no-prepare")]
    env, pins = need_env(project)
    src = fwlib.src_dir(project, pins) if skip_prep else prepare(project, pins, env)
    if not (src / "build" / "CMakeCache.txt").is_file() and not (src / "sdkconfig").is_file():
        # A fresh tree: pick the target first, which also writes sdkconfig from sdkconfig.defaults.
        rc = run_idf(env, src, ["set-target", pins["target"]])
        if rc:
            return rc
    overlay = pins.get("qemu", {}).get("sdkconfig")
    if qemu and overlay:
        bq = src / "build-qemu"
        bq.mkdir(exist_ok=True)
        base = (src / "sdkconfig").read_text(encoding="utf-8") if (src / "sdkconfig").is_file() else ""
        want = overlay_sdkconfig(base, overlay, sdkconfig_renames(Path(env["IDF_PATH"]), src))
        sc, stamp = bq / "sdkconfig", bq / ".fw-overlay"
        # IDF rewrites sdkconfig as it configures: compare what we wrote last.
        if not sc.is_file() or not stamp.is_file() or stamp.read_text(encoding="utf-8") != want:
            sc.write_text(want, encoding="utf-8")
            stamp.write_text(want, encoding="utf-8")
        return run_idf(env, src, ["-B", str(bq), "-D", f"SDKCONFIG={sc}"] + rest + ["build"])
    return run_idf(env, src, rest + ["build"])


def port_arg(rest: list[str]) -> tuple[str | None, list[str]]:
    port = os.environ.get("FW_PORT")
    out = []
    i = 0
    while i < len(rest):
        if rest[i] in ("-p", "--port") and i + 1 < len(rest):
            port = rest[i + 1]
            i += 2
            continue
        out.append(rest[i])
        i += 1
    return port, out


def cmd_flash(args: list[str]) -> int:
    project, rest = split_project(args)
    port, rest = port_arg(rest)
    if not port:
        return die("no port: --port /dev/ttyUSB0 or rfc2217://host:port, or set FW_PORT (fw ports lists them)")
    env, pins = need_env(project)
    return idf_py(env, fwlib.src_dir(project, pins), ["-p", port] + rest + ["flash"])


def cmd_monitor(args: list[str]) -> int:
    import argparse
    project, rest = split_project(args)
    ap = argparse.ArgumentParser(prog="fw monitor")
    ap.add_argument("-p", "--port", default=os.environ.get("FW_PORT"))
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--seconds", type=float, default=15)
    ap.add_argument("--expect", default=bootlog.DEFAULT_EXPECT)
    ap.add_argument("--no-reset", action="store_true", help="don't pulse EN to reboot the chip first")
    ap.add_argument("--log", type=Path)
    a = ap.parse_args(rest)
    if not a.port:
        return die("no port: --port or FW_PORT (fw ports lists them)")
    env, _ = need_env(project)
    # pyserial lives in the IDF's venv: re-run this under it.
    if "serial" not in sys.modules and os.environ.get("FW_IN_IDF_PY") != "1":
        py = os.path.join(env["IDF_PYTHON_ENV_PATH"], "bin", "python")
        env["FW_IN_IDF_PY"] = "1"
        os.execve(py, [py, __file__, "monitor", str(project)] + rest, env)
    import serial  # noqa: PLC0415
    s = serial.serial_for_url(a.port, a.baud, timeout=0.2)
    if not a.no_reset:  # EN low then high through the usual DTR/RTS auto-reset circuit
        s.dtr = False
        s.rts = True
        time.sleep(0.1)
        s.rts = False
    buf = b""
    end = time.monotonic() + a.seconds
    while time.monotonic() < end:
        buf += s.read(4096)
        v = bootlog.judge(buf.decode(errors="replace"), a.expect, done=False)
        if not v.ok and v.reason != "still booting":
            break
    s.close()
    text = buf.decode(errors="replace")
    log = a.log or project / "build" / "fw-monitor.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(text, encoding="utf-8")
    sys.stdout.write(text[-4000:])
    v = bootlog.judge(text, a.expect)
    print(f"\nfw monitor: {v}  (log: {log})")
    return 0 if v.ok else 1


def list_ports() -> list[str]:
    pats = ["/dev/serial/by-id/*", "/dev/ttyUSB*", "/dev/ttyACM*"]
    out = []
    for p in pats:
        out += sorted(glob.glob(p))
    return out


def cmd_ports(args: list[str]) -> int:
    ports = list_ports()
    for p in ports:
        print(p)
    if os.environ.get("FW_PORT"):
        print(f"FW_PORT={os.environ['FW_PORT']}")
    if not ports and not os.environ.get("FW_PORT"):
        print("fw ports: no serial port here. A board on this machine needs a warden to pass its "
              "/dev/ttyUSB* into the sandbox; one elsewhere can be served with esp_rfc2217_server.py "
              "and flashed as rfc2217://host:port.", file=sys.stderr)
        return 1
    return 0


def cmd_doctor(args: list[str]) -> int:
    project, _ = split_project(args)
    pins = fwlib.read_pins(project)
    print(f"project   {project}")
    print(f"pins      idf {pins['idf']}, target {pins['target']}"
          + (f", host_tests {pins['host_tests']}" if pins.get("host_tests") else ""))
    for r in fwlib.roots():
        have = sorted(p.name[len("esp-idf-"):] for p in r.glob("esp-idf-*") if (p / ".fw-ok").is_file())
        print(f"root      {r}: {', '.join(have) or '-'}")
    root = fwlib.find_idf(pins["idf"])
    print(f"idf       {root / ('esp-idf-' + pins['idf']) if root else 'MISSING (fw setup)'}")
    q = fwlib.find_qemu()
    print(f"qemu      {q or 'MISSING (fw setup)'}")
    print(f"ports     {', '.join(list_ports()) or os.environ.get('FW_PORT') or '-'}")
    return 0 if root else 1


COMMANDS = {"env": cmd_env, "idf": cmd_idf, "build": cmd_build, "flash": cmd_flash,
            "monitor": cmd_monitor, "ports": cmd_ports, "doctor": cmd_doctor}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        return 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
