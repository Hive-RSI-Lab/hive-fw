"""fw setup: install an ESP-IDF release, its toolchains and Espressif's QEMU.

    fw setup [--idf v4.4.8] [--target esp32] [--prefix DIR] [--project DIR] [--no-qemu]

The IDF version comes from --idf, else the project's pins (fw.json,
.idf-version, a Dockerfile's IDF tag), else fwlib.DEFAULT_IDF. Everything
goes under one prefix (default $FW_HOME, ~/.cache/fw): nothing outside it is
touched, so an agent runs it in its own HOME and a warden runs it once with
--prefix /hive/tools/fw for everyone. It does nothing for a version some root
already has. A lock on the prefix keeps two agents from installing at once.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fwlib  # noqa: E402


def log(msg: str) -> None:
    print(f"fw setup: {msg}", file=sys.stderr, flush=True)


def fetch(url: str, sha256: str, dest: Path) -> Path:
    """Download url to dest and check its sha256; a mismatch removes it."""
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    got = hashlib.sha256(dest.read_bytes()).hexdigest()
    if got != sha256:
        dest.unlink()
        raise RuntimeError(f"{url}: sha256 {got}, want {sha256}")
    return dest


def uv() -> str:
    u = shutil.which("uv") or "/hive/tools/uv/bin/uv"
    if not os.path.exists(u):
        raise RuntimeError("needs uv (hive agent install --tool uv)")
    return u


def find_python(minor: str) -> str:
    """A CPython of this minor version, through uv (it downloads one into
    uv's own folder if none is installed)."""
    subprocess.run([uv(), "python", "install", minor], check=True, stdout=sys.stderr)
    return subprocess.run([uv(), "python", "find", minor], check=True,
                          capture_output=True, text=True).stdout.strip()


def install_qemu(prefix: Path) -> Path:
    dest = prefix / f"qemu-{fwlib.QEMU['tag']}"
    q = dest / "qemu" / "bin" / "qemu-system-xtensa"
    if q.is_file():
        return q
    with tempfile.TemporaryDirectory(dir=prefix) as tmp:
        t = Path(tmp)
        log("fetching Espressif QEMU " + fwlib.QEMU["tag"])
        tar = fetch(fwlib.QEMU["url"], fwlib.QEMU["sha256"], t / "qemu.tar.xz")
        (t / "out").mkdir()
        with tarfile.open(tar) as tf:
            tf.extractall(t / "out", filter="data")
        deb = fetch(fwlib.SLIRP["url"], fwlib.SLIRP["sha256"], t / "slirp.deb")
        unpack_deb_libs(deb, t / "out" / "lib")
        if dest.exists():
            shutil.rmtree(dest)
        (t / "out").rename(dest)
    return q


def unpack_deb_libs(deb: Path, lib: Path) -> None:
    """The shared libraries out of a .deb (an ar archive holding data.tar.*),
    flat into lib/."""
    lib.mkdir(parents=True, exist_ok=True)
    work = deb.parent / "deb"
    work.mkdir()
    subprocess.run(["ar", "x", str(deb)], cwd=work, check=True)
    data = next(work.glob("data.tar.*"))
    with tarfile.open(data) as tf:
        for m in tf.getmembers():
            if ".so" in Path(m.name).name and (m.isfile() or m.issym()):
                m.name = Path(m.name).name
                tf.extract(m, lib, filter="data")


def install_idf(prefix: Path, idf: str, targets: str) -> Path:
    src = prefix / f"esp-idf-{idf}"
    tools = prefix / "espressif"
    if not (src / "tools" / "idf_tools.py").is_file():
        if src.exists():
            shutil.rmtree(src)
        log(f"cloning ESP-IDF {idf} (with submodules, shallow)")
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", idf,
                        "--recursive", "--shallow-submodules", "--jobs", "8",
                        "https://github.com/espressif/esp-idf.git", str(src)],
                       check=True, stdout=sys.stderr)
    else:  # a clone an earlier run left: finish its submodules (a no-op when done)
        subprocess.run(["git", "-C", str(src), "submodule", "update", "-q", "--init", "--recursive",
                        "--depth", "1", "--jobs", "8"], check=True, stdout=sys.stderr)
    py = find_python(fwlib.python_for(idf))
    env = dict(os.environ, IDF_PATH=str(src), IDF_TOOLS_PATH=str(tools))
    env.pop("VIRTUAL_ENV", None)
    it = [py, str(src / "tools" / "idf_tools.py"), "--non-interactive"]
    log(f"installing {idf} toolchains for {targets}, cmake and ninja")
    subprocess.run(it + ["install", f"--targets={targets}", "required", "cmake", "ninja"],
                   env=env, check=True, stdout=sys.stderr)
    # idf_tools would make its venv with `pip install --user virtualenv`, which
    # uv's pythons refuse (PEP 668); a seeded venv at the path it looks for
    # is taken as is, and it installs the IDF's requirements into it.
    mm = ".".join(idf[1:].split("-")[0].split(".")[:2])
    pyenv = tools / "python_env" / f"idf{mm}_py{fwlib.python_for(idf)}_env"
    if not (pyenv / "bin" / "python").exists():
        subprocess.run([uv(), "venv", "-q", "--seed", "--python", py, str(pyenv)],
                       check=True, stdout=sys.stderr)
    log("installing the IDF's python requirements")
    subprocess.run(it + ["install-python-env"], env=env, check=True, stdout=sys.stderr)
    (src / ".fw-ok").write_text(str(pyenv) + "\n", encoding="utf-8")
    return src


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fw setup", description=__doc__.split("\n")[0])
    ap.add_argument("--idf")
    ap.add_argument("--target", help="comma list, e.g. esp32,esp32s3 (default: the project's)")
    ap.add_argument("--prefix", type=Path)
    ap.add_argument("--project", type=Path, default=Path.cwd())
    ap.add_argument("--no-qemu", action="store_true")
    a = ap.parse_args(argv)
    pins = fwlib.read_pins(a.project)
    idf = fwlib.check_version(a.idf or pins["idf"])
    targets = a.target or pins["target"]
    prefix = (a.prefix or fwlib.home_root()).resolve()
    have = fwlib.find_idf(idf)
    if have and not a.prefix:
        log(f"ESP-IDF {idf} is ready in {have}")
    else:
        prefix.mkdir(parents=True, exist_ok=True)
        with open(prefix / ".fw-lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            if not fwlib.idf_ready(prefix, idf):
                install_idf(prefix, idf, targets)
            log(f"ESP-IDF {idf} is ready in {prefix}")
    if not a.no_qemu and not fwlib.find_qemu():
        prefix.mkdir(parents=True, exist_ok=True)
        with open(prefix / ".fw-lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            log(f"QEMU ready: {install_qemu(prefix)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
