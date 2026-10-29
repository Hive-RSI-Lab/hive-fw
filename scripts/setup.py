"""fw setup: install an ESP-IDF release, its toolchains and Espressif's QEMU.

    fw setup [PROJECT] [--idf v4.4.8] [--target esp32] [--prefix DIR] [--no-qemu]

The IDF version comes from --idf, else the project's pins (fw.json,
.idf-version, a Dockerfile's IDF tag), else the fw.json files in its
subfolders (each version they pin), else fwlib.DEFAULT_IDF. Submodules the
Hive's proxy refuses over git come from GitHub's codeload tarball of the
pinned commit. Everything
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


def find_python(minor: str, prefix: Path) -> str:
    """A CPython of this minor version, installed by uv under the prefix, so
    the IDF's venv points at nothing outside it (a shared prefix must work
    from every sandbox, where another HOME's uv pythons aren't)."""
    env = dict(os.environ, UV_PYTHON_INSTALL_DIR=str(prefix / "python"))
    subprocess.run([uv(), "python", "install", minor], env=env, check=True, stdout=sys.stderr)
    return subprocess.run([uv(), "python", "find", "--managed-python", minor], env=env, check=True,
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


def submodules(repo: Path) -> list[tuple[str, str, str]]:
    """(name, path, url) for each submodule .gitmodules names, relative URLs
    resolved against the repo's origin."""
    if not (repo / ".gitmodules").is_file():
        return []
    r = subprocess.run(["git", "config", "-f", str(repo / ".gitmodules"), "--get-regexp",
                        r"^submodule\..*\.(path|url)$"], capture_output=True, text=True)
    mods: dict[str, dict[str, str]] = {}
    for line in r.stdout.splitlines():
        key, _, val = line.partition(" ")
        name, _, field = key[len("submodule."):].rpartition(".")
        mods.setdefault(name, {})[field] = val.strip()
    out = []
    for name, m in mods.items():
        url = m.get("url", "")
        if url.startswith("../") or url.startswith("./"):
            base = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"],
                                  capture_output=True, text=True).stdout.strip()
            for part in url.split("/"):
                if part == "..":
                    base = base.rsplit("/", 1)[0]
                elif part not in (".", ""):
                    base += "/" + part
            url = base
        if "path" in m:
            out.append((name, m["path"], url))
    return out


def codeload_url(url: str, sha: str) -> str | None:
    """GitHub's tarball of a commit, for a github.com clone URL (the Hive's
    proxy serves codeload.github.com where it refuses some repos' git)."""
    for pre in ("https://github.com/", "http://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if url.startswith(pre):
            repo = url[len(pre):].rstrip("/").removesuffix(".git")
            if repo.count("/") == 1:
                return f"https://codeload.github.com/{repo}/tar.gz/{sha}"
    return None


def download(url: str, dest: Path) -> Path:
    with urllib.request.urlopen(url, timeout=300) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest


def unpack_tarball(tar: Path, dest: Path) -> None:
    """A codeload tarball (one top folder, <repo>-<sha>/) into dest, whole or not at all."""
    work = Path(tempfile.mkdtemp(dir=dest.parent, prefix=".fw-unpack-"))
    try:
        with tarfile.open(tar) as tf:
            tf.extractall(work, filter="data")
        tops = list(work.iterdir())
        top = tops[0] if len(tops) == 1 and tops[0].is_dir() else work
        if dest.exists():
            shutil.rmtree(dest)
        top.rename(dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def fill_submodules(repo: Path) -> None:
    """Check out every submodule at its pinned commit, shallow and nested.
    One the proxy refuses over git (it does github.com/nghttp2/nghttp2, an
    IDF submodule cmake needs) comes from GitHub's codeload tarball of that
    same commit, marked update=none so later `git submodule update`s skip it;
    a tarball carries no nested submodules of its own."""
    for name, path, url in submodules(repo):
        sub = repo / path
        if (sub / ".git").exists() or (sub / ".fw-tarball").is_file():
            if (sub / ".git").exists():
                fill_submodules(sub)
            continue
        r = subprocess.run(["git", "-C", str(repo), "submodule", "update", "-q", "--init", "--depth", "1",
                            "--", path], stdout=sys.stderr, stderr=subprocess.PIPE, text=True)
        if r.returncode == 0:
            fill_submodules(sub)
            continue
        tree = subprocess.run(["git", "-C", str(repo), "ls-tree", "HEAD", "--", path],
                              capture_output=True, text=True, check=True).stdout.split()
        sha = tree[2] if len(tree) >= 3 and tree[1] == "commit" else ""
        tar_url = codeload_url(url, sha) if sha else None
        if not tar_url:
            raise RuntimeError(f"submodule {path} ({url}) failed and has no tarball to fall back on:\n{r.stderr.strip()}")
        log(f"git refused {url}; unpacking {tar_url}")
        with tempfile.TemporaryDirectory(dir=repo) as tmp:
            unpack_tarball(download(tar_url, Path(tmp) / "src.tar.gz"), sub)
        (sub / ".fw-tarball").write_text(f"{tar_url}\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "config", f"submodule.{name}.update", "none"], check=True)


def install_idf(prefix: Path, idf: str, targets: str) -> Path:
    src = prefix / f"esp-idf-{idf}"
    tools = prefix / "espressif"
    if not (src / "tools" / "idf_tools.py").is_file():
        if src.exists():
            shutil.rmtree(src)
        log(f"cloning ESP-IDF {idf} (shallow)")
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", idf,
                        "https://github.com/espressif/esp-idf.git", str(src)],
                       check=True, stdout=sys.stderr)
    # One submodule at a time (a no-op for those an earlier run left), so one
    # the proxy refuses falls back on its tarball instead of failing the rest.
    log("checking out its submodules")
    bulk = subprocess.run(["git", "-C", str(src), "submodule", "update", "-q", "--init", "--recursive",
                           "--depth", "1", "--jobs", "8"], stdout=sys.stderr, stderr=subprocess.PIPE, text=True)
    if bulk.returncode:
        log("some submodules didn't come over git; trying them one at a time")
    fill_submodules(src)
    py = find_python(fwlib.python_for(idf), prefix)
    env = dict(os.environ, IDF_PATH=str(src), IDF_TOOLS_PATH=str(tools))
    env.pop("VIRTUAL_ENV", None)
    it = [py, str(src / "tools" / "idf_tools.py"), "--non-interactive"]
    log(f"installing {idf} toolchains for {targets}, cmake and ninja")
    subprocess.run(it + ["install", f"--targets={targets}", "required"],
                   env=env, check=True, stdout=sys.stderr)
    # On Linux idf_tools leaves cmake and ninja to the OS unless named, and
    # names with "required" install only the required ones; the IDF's own
    # pins matter (cmake 3.31 orders IDF 4.4's -std flags so C++20 code fails).
    subprocess.run(it + ["install", "cmake", "ninja"], env=env, check=True, stdout=sys.stderr)
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


SKIP = {"node_modules", "build", "managed_components", "esp-idf"}


def project_pins(project: Path, depth: int = 3) -> list[tuple[Path, dict]]:
    """The pins setup installs for: the project's own (fw.json, .idf-version
    or a Dockerfile), else each fw.json in its subfolders (a repo whose
    firmware lives in firmware/), else the defaults."""
    if (project / "fw.json").is_file() or fwlib.detect_idf(project):
        return [(project, fwlib.read_pins(project))]
    found = []

    def walk(d: Path, left: int) -> None:
        for c in sorted(d.iterdir()):
            if not c.is_dir() or c.is_symlink() or c.name.startswith(".") or c.name in SKIP:
                continue
            if (c / "fw.json").is_file():
                found.append((c, fwlib.read_pins(c)))
            elif left > 1:
                walk(c, left - 1)

    walk(project, depth)
    return found or [(project, fwlib.read_pins(project))]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fw setup", description=__doc__.split("\n")[0])
    ap.add_argument("project", nargs="?", type=Path, help="default: --project, else the current directory")
    ap.add_argument("--idf")
    ap.add_argument("--target", help="comma list, e.g. esp32,esp32s3 (default: the project's)")
    ap.add_argument("--prefix", type=Path)
    ap.add_argument("--project", dest="project_opt", type=Path)
    ap.add_argument("--no-qemu", action="store_true")
    a = ap.parse_args(argv)
    project = (a.project or a.project_opt or Path.cwd()).resolve()
    if not project.is_dir():
        ap.error(f"no project directory {project}")
    found = project_pins(project)
    want: dict[str, set[str]] = {}  # each IDF version, with the targets that need it
    for where, pins in found:
        if where != project:
            log(f"pins from {where / 'fw.json'}: IDF {pins['idf']}, {pins['target']}")
        want.setdefault(a.idf or pins["idf"], set()).add(pins["target"])
    prefix = (a.prefix or fwlib.home_root()).resolve()
    for idf, ts in want.items():
        idf = fwlib.check_version(idf)
        targets = a.target or ",".join(sorted(ts))
        have = fwlib.find_idf(idf)
        if have and not a.prefix:
            log(f"ESP-IDF {idf} is ready in {have}")
            continue
        prefix.mkdir(parents=True, exist_ok=True)
        with open(prefix / ".fw-lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            if not fwlib.idf_ready(prefix, idf):
                install_idf(prefix, idf, targets)
            log(f"ESP-IDF {idf} is ready in {prefix}")
    # With --prefix (a shared install) QEMU goes there too, whatever other roots have.
    has_qemu = (prefix / f"qemu-{fwlib.QEMU['tag']}" / "qemu" / "bin" / "qemu-system-xtensa").is_file() if a.prefix else fwlib.find_qemu()
    if not a.no_qemu and not has_qemu:
        prefix.mkdir(parents=True, exist_ok=True)
        with open(prefix / ".fw-lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            log(f"QEMU ready: {install_qemu(prefix)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
