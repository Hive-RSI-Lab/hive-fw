"""Shared bits for the fw scripts: where toolchains live, a project's pins,
and the ESP-IDF environment. Standard library only: these scripts run with
whatever python3 is on PATH, before any IDF venv exists.

Toolchains are looked up, in order, in:
  $FW_TOOLS          (an explicit root)
  $HIVE_TOOLS/fw     (the Hive's shared folder, /hive/tools/fw, put there by a warden)
  $FW_HOME           (default $HOME/.cache/fw, the agent's own)
Each root holds esp-idf-<version>/ (an IDF checkout), espressif/ (IDF_TOOLS_PATH:
compilers, the IDF python env) and qemu-<tag>/.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# The IDF a project gets when it pins none. squeezelite-esp32 pins v4.4.8.
DEFAULT_IDF = "v5.5.6"
DEFAULT_TARGET = "esp32"

# Espressif's QEMU (ESP32 and ESP32-S3 machines) and the one library it
# needs that Ubuntu 26.04 doesn't ship by default.
QEMU = {
    "tag": "esp-develop-9.2.2-20260417",
    "url": "https://github.com/espressif/qemu/releases/download/esp-develop-9.2.2-20260417/"
           "qemu-xtensa-softmmu-esp_develop_9.2.2_20260417-x86_64-linux-gnu.tar.xz",
    "sha256": "0eecb2a34a5586c0e59110f77b9343b7b336e82fdb0e1a30e1dc1bab8a547e35",
}
SLIRP = {
    "url": "http://archive.ubuntu.com/ubuntu/pool/main/libs/libslirp/libslirp0_4.7.0-1ubuntu3_amd64.deb",
    "sha256": "1da5239a2829ad5e91da02e8a56fd3cb8cff116e7adafeb07f9c9d6e7804f0f3",
}

_VERSION = re.compile(r"^v\d+\.\d+(\.\d+)?(-[a-z0-9.]+)?$")


def check_version(v: str) -> str:
    """An IDF release tag like v4.4.8 or v5.5.6; anything else is refused
    (it becomes a path and a git ref)."""
    if not _VERSION.match(v or ""):
        raise ValueError(f"not an ESP-IDF release tag: {v!r} (want e.g. v5.5.6)")
    return v


def python_for(idf: str) -> str:
    """The python minor version an IDF release's own venv is built with.
    IDF 4.x gets 3.10: the last with wheels for the packages its projects pin
    (squeezelite-esp32's cspot needs grpcio-tools 1.48, cp310 at newest); 5.x and 6.x take 3.12."""
    major = int(check_version(idf)[1:].split(".")[0])
    return "3.10" if major < 5 else "3.12"


def home_root() -> Path:
    return Path(os.environ.get("FW_HOME") or Path.home() / ".cache" / "fw")


def roots() -> list[Path]:
    out = []
    if os.environ.get("FW_TOOLS"):
        out.append(Path(os.environ["FW_TOOLS"]))
    out.append(Path(os.environ.get("HIVE_TOOLS", "/hive/tools")) / "fw")
    out.append(home_root())
    return out


def idf_ready(root: Path, idf: str) -> bool:
    return (root / f"esp-idf-{idf}" / ".fw-ok").is_file()


def find_idf(idf: str) -> Path | None:
    """The root holding a finished install of this IDF version, or None."""
    for r in roots():
        if idf_ready(r, idf):
            return r
    return None


def find_qemu() -> Path | None:
    """qemu-system-xtensa from the first root that has the pinned build."""
    for r in roots():
        q = r / f"qemu-{QEMU['tag']}" / "qemu" / "bin" / "qemu-system-xtensa"
        if q.is_file():
            return q
    return None


def qemu_env(qemu: Path) -> dict[str, str]:
    lib = qemu.parent.parent.parent / "lib"
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = str(lib) + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    return env


def read_pins(project: Path) -> dict:
    """A project's fw.json (all keys optional):
        {"idf": "v4.4.8", "target": "esp32", "host_tests": "test/host",
         "prepare": "tools/fetch.sh {src}", "src": "{project}/.fw-src",
         "python": ["setuptools<70"],
         "qemu": {"expect": "regex", "timeout": 30, "psram": "4M", "flash": "8MB",
                  "sdkconfig": ["CONFIG_ESP32_REV_MIN_0=y"]}}
    "prepare" is for firmware that is an overlay on an upstream tree (the
    receiver's is squeezelite-esp32 plus a patch): fw build runs it in the
    project dir to make the IDF project at "src", then builds there.
    "python" names extra packages the build needs beside the IDF's own.
    With no fw.json, the IDF pin is read from .idf-version or from a
    Dockerfile's IDF_CLONE_BRANCH_OR_TAG (squeezelite-esp32 pins it there)."""
    pins: dict = {}
    f = project / "fw.json"
    if f.is_file():
        pins = json.loads(f.read_text(encoding="utf-8"))
    if "idf" not in pins:
        v = detect_idf(project)
        if v:
            pins["idf"] = v
    pins.setdefault("idf", DEFAULT_IDF)
    pins.setdefault("target", DEFAULT_TARGET)
    check_version(pins["idf"])
    return pins


def src_dir(project: Path, pins: dict) -> Path:
    """Where the IDF project is: the project itself, or the tree its
    "prepare" step makes (default PROJECT/.fw-src)."""
    if not pins.get("prepare"):
        return project
    return Path(expand(pins.get("src") or "{project}/.fw-src", project)).resolve()


def expand(s: str, project: Path, src: Path | None = None) -> str:
    return (s.replace("{project}", str(project)).replace("{home}", str(Path.home()))
            .replace("{src}", str(src) if src else "{src}"))


def qemu_build_dir(src: Path, pins: dict) -> Path:
    """A firmware whose QEMU boot needs config of its own (a chip-revision
    floor QEMU meets, say) gets a second build there; else the build itself."""
    return src / "build-qemu" if pins.get("qemu", {}).get("sdkconfig") else src / "build"


def detect_idf(project: Path) -> str | None:
    f = project / ".idf-version"
    if f.is_file():
        return f.read_text(encoding="utf-8").strip() or None
    d = project / "Dockerfile"
    if d.is_file():
        m = re.search(r"IDF_CLONE_BRANCH_OR_TAG=(v[0-9][\w.\-]*)", d.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(1)
    return None


def idf_env(root: Path, idf: str, extras: list[str] | None = None) -> dict[str, str]:
    """The environment export.sh would set, without sourcing a shell script:
    IDF_PATH, IDF_TOOLS_PATH, and PATH with the toolchains and the IDF venv."""
    idf_path = root / f"esp-idf-{idf}"
    tools = root / "espressif"
    env = dict(os.environ)
    env.update(IDF_PATH=str(idf_path), IDF_TOOLS_PATH=str(tools))
    env.pop("VIRTUAL_ENV", None)
    pyenv = Path((idf_path / ".fw-ok").read_text(encoding="utf-8").strip())
    out = subprocess.run(
        [str(pyenv / "bin" / "python"), str(idf_path / "tools" / "idf_tools.py"),
         "export", "--format", "key-value"],
        env=env, check=True, capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.replace("$PATH", env.get("PATH", "")).replace("%PATH%", env.get("PATH", ""))
        env[k] = v
    env["IDF_PYTHON_ENV_PATH"] = str(pyenv)
    env["PATH"] = f"{pyenv}/bin:{idf_path}/tools:{env['PATH']}"
    # ninja runs cores+2 jobs and reads no setting for it; on a shared machine
    # that's too many, so a ninja first on PATH caps it at FW_JOBS (default 4).
    shim = jobs_shim(pyenv / "bin" / "ninja", int(env.get("FW_JOBS") or 4))
    env["PATH"] = f"{shim}:{env['PATH']}"
    if extras:
        d = python_extras(pyenv, extras)
        env["PYTHONPATH"] = str(d) + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def python_extras(pyenv: Path, reqs: list[str]) -> Path:
    """Packages a firmware's build needs beside the IDF's (squeezelite's cspot
    wants setuptools<70 and protobuf<4), installed once into a folder of the
    agent's own and put first on PYTHONPATH: the IDF venv may be the shared,
    read-only one, and what's here wins over its own copies."""
    key = hashlib.sha256(("\0".join([str(pyenv)] + sorted(reqs))).encode()).hexdigest()[:16]
    d = home_root() / "pyextra" / key
    if not (d / ".fw-ok").is_file():
        import shutil
        uv = shutil.which("uv") or "/hive/tools/uv/bin/uv"
        subprocess.run([uv, "pip", "install", "-q", "--python", str(pyenv / "bin" / "python"),
                        "--target", str(d)] + reqs, check=True, stdout=sys.stderr)
        (d / ".fw-ok").write_text("\n".join(reqs) + "\n")
    return d


def jobs_shim(ninja: Path, jobs: int) -> Path:
    d = home_root() / "shims" / f"j{jobs}"
    f = d / "ninja"
    body = f'#!/bin/sh\nexec "{ninja}" -j {jobs} "$@"\n'
    if not f.is_file() or f.read_text() != body:
        d.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
        f.chmod(0o755)
    return d
