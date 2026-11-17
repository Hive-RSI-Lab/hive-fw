"""fw fuzz: libFuzzer harnesses for a firmware's portable code, on the host.

    fw fuzz [PROJECT] [--dir test/fuzz] [--secs 10] [--only NAME] [--jobs N]
    fw fuzz [PROJECT] --new NAME        write a harness from the template
    fw fuzz [PROJECT] --replay NAME FILE   run one input through a harness

Each fuzz_*.c in the fuzz dir (fw.json "fuzz_tests", default test/fuzz) is
one harness: it defines LLVMFuzzerTestOneInput and names its sources in the
same header lines as a host test (// fw-src: main/x.c, // fw-inc: main,
// fw-cflags: -DX, // fw-ldflags: -lcrypto). fw builds it with clang,
libFuzzer, AddressSanitizer and UBSan, and runs it for --secs seconds, all
harnesses at once with --jobs. The corpus grows in build-fuzz/corpus/NAME
from the seeds in the fuzz dir's seeds/NAME; the dictionary is the string
literals of the harness's sources plus NAME.dict beside it. A crash leaves
its input in build-fuzz/crash-NAME-*; check it in as seeds/NAME/regress-*
once fixed, so every later run replays it.

clang comes from $CLANG, ~/.local/opt/LLVM-*/bin/clang, or PATH. With no
clang that links libFuzzer, fw fuzz says so and exits 3.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fwlib  # noqa: E402
import hosttest  # noqa: E402

TEMPLATE = """\
// A libFuzzer harness, run by `fw fuzz`: it feeds the bytes to the code under
// test and must never crash, whatever they are. Name the sources it links:
// fw-src: main/{name}.c
// fw-inc: main
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Checks an invariant: a failure is reported as a crash, with the input. */
#define FUZZ_ASSERT(c) do {{ if (!(c)) {{ fprintf(stderr, "FUZZ_ASSERT %s:%d %s\\n", \\
    __FILE__, __LINE__, #c); abort(); }} }} while (0)

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{{
    /* Spend the first byte on choices the input can't express by itself (a
     * mode, a flag per option), then hand the rest over as text: */
    if (size < 1) {{
        return 0;
    }}
    uint8_t flags = data[0];
    char *text = malloc(size);   /* NUL-terminated copy: the code may expect a C string */
    memcpy(text, data + 1, size - 1);
    text[size - 1] = 0;
    (void)flags;
    /* call the code under test with text here, and FUZZ_ASSERT what must hold */
    free(text);
    return 0;
}}
"""

_LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"')


def find_clang() -> str | None:
    """The first clang that links libFuzzer: $CLANG, ~/.local/opt/LLVM-*/bin/clang, PATH."""
    cands = [os.environ.get("CLANG", "")]
    cands += sorted(glob.glob(os.path.expanduser("~/.local/opt/LLVM-*/bin/clang")), reverse=True)
    cands += [shutil.which("clang") or ""]
    for c in cands:
        if not c or not (os.path.isfile(c) or shutil.which(c)):
            continue
        r = subprocess.run([c, "-x", "c", "-", "-fsanitize=fuzzer", "-o", os.devnull],
                           input="int LLVMFuzzerTestOneInput(const char*d,long n){return 0;}",
                           capture_output=True, text=True)
        if r.returncode == 0:
            return c
    return None


def dictionary(sources: list[Path]) -> list[str]:
    """libFuzzer dictionary entries: the string literals the sources compare against."""
    seen: dict[str, None] = {}
    for p in sources:
        try:
            src = p.read_text(errors="replace")
        except OSError:
            continue
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        for m in _LITERAL.finditer(src):
            s = m.group(1)
            if not 2 <= len(s) <= 40 or "%" in s or "\\" in s:
                continue
            for tok in sorted({s, *re.findall(r"[A-Za-z_/][A-Za-z0-9_/.-]+", s)}):
                seen.setdefault(tok, None)
    return [f'"{t}"' for t in seen]


def build_cmd(clang: str, project: Path, fdir: Path, harness: Path, out: Path) -> list[str]:
    d = hosttest.directives(harness)
    cmd = [clang, "-std=gnu11", "-g", "-O1", "-DFW_FUZZ=1",
           "-fsanitize=fuzzer,address,undefined,float-cast-overflow", "-fno-sanitize-recover=all",
           "-I", str(fdir)]
    for i in d["inc"]:
        cmd += ["-I", str(project / i)]
    cmd += d["cflags"] + [str(harness)] + [str(project / s) for s in d["src"]]
    return cmd + ["-o", str(out), "-lm"] + d["ldflags"]


def prepare(clang: str, project: Path, fdir: Path, h: Path, bdir: Path) -> tuple[Path, list[str]] | str:
    """Build one harness and its dictionary; return (exe, run args) or the build error."""
    name = h.stem
    exe = bdir / name
    b = subprocess.run(build_cmd(clang, project, fdir, h, exe), capture_output=True, text=True)
    if b.returncode:
        return b.stderr.strip() or "build failed"
    srcs = [project / s for s in hosttest.directives(h)["src"]]
    lines = dictionary(srcs)
    extra = fdir / f"{name}.dict"
    if extra.is_file():
        lines += [ln for ln in extra.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    dfile = bdir / f"{name}.dict"
    dfile.write_text("\n".join(lines) + "\n")
    corpus = bdir / "corpus" / name
    corpus.mkdir(parents=True, exist_ok=True)
    seeds = fdir / "seeds" / name
    args = [str(exe), f"-artifact_prefix={bdir}/crash-{name}-", "-rss_limit_mb=1024"]
    if lines:
        args.append(f"-dict={dfile}")
    args.append(str(corpus))
    if seeds.is_dir():
        args.append(str(seeds))
    return exe, args


def verdict(name: str, rc: int, log: Path, secs: int) -> tuple[bool, str]:
    text = log.read_text(errors="replace")
    runs = re.findall(r"Done (\d+) runs", text)
    cov = re.findall(r"cov: (\d+)", text)
    if rc == 0:
        more = f"{runs[-1]} runs" if runs else "done"
        return True, f"fuzz {name} ({secs} s): {more}, coverage {cov[-1] if cov else '?'}, no crash"
    lines = text.splitlines()
    first = next((i for i, ln in enumerate(lines)
                  if re.search(r"==\d+==ERROR|runtime error|FUZZ_ASSERT|deadly signal", ln)), None)
    detail = "\n".join(lines[first:first + 30]) if first is not None else "\n".join(lines[-30:])
    crash = re.findall(r"Test unit written to (\S+)", text)
    rep = f"\n  replay: fw fuzz --replay {name} {crash[-1]}" if crash else ""
    return False, f"fuzz {name}: CRASH (log {log}){rep}\n{detail}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fw fuzz", description=__doc__.split("\n")[0])
    ap.add_argument("project", nargs="?", type=Path, default=Path.cwd())
    ap.add_argument("--dir")
    ap.add_argument("--secs", type=int, default=10)
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--new", metavar="NAME")
    ap.add_argument("--replay", nargs=2, metavar=("NAME", "FILE"))
    a = ap.parse_args(argv)
    project = a.project.resolve()
    fdir = project / (a.dir or fwlib.read_pins(project).get("fuzz_tests") or "test/fuzz")
    if a.new:
        name = a.new if a.new.startswith("fuzz_") else f"fuzz_{a.new}"
        dest = fdir / f"{name}.c"
        if dest.exists():
            print(f"fw fuzz: {dest} exists", file=sys.stderr)
            return 2
        fdir.mkdir(parents=True, exist_ok=True)
        (fdir / "seeds" / name).mkdir(parents=True, exist_ok=True)
        dest.write_text(TEMPLATE.format(name=name[5:]))
        print(f"wrote {dest} and {fdir / 'seeds' / name}/: fill in its fw-src lines and the call")
        return 0
    harnesses = sorted(fdir.glob("fuzz_*.c"))
    if a.only or a.replay:
        want = set(a.only) | ({a.replay[0]} if a.replay else set())
        harnesses = [h for h in harnesses if h.stem in want or h.stem[5:] in want]
    if not harnesses:
        print(f"fw fuzz: no fuzz_*.c in {fdir}" + (" matching" if a.only or a.replay else ""), file=sys.stderr)
        return 2
    clang = find_clang()
    if not clang:
        print("fw fuzz: SKIPPED, no clang with libFuzzer (set CLANG)", file=sys.stderr)
        return 3
    bdir = project / "build-fuzz"
    bdir.mkdir(exist_ok=True)
    if a.replay:
        got = prepare(clang, project, fdir, harnesses[0], bdir)
        if isinstance(got, str):
            print(f"fuzz {harnesses[0].stem}: BUILD FAILED\n{got}")
            return 1
        return subprocess.run([str(got[0]), a.replay[1]]).returncode
    ok = True
    ready = []
    for h in harnesses:
        got = prepare(clang, project, fdir, h, bdir)
        if isinstance(got, str):
            print(f"fuzz {h.stem}: BUILD FAILED\n{got}", flush=True)
            ok = False
        else:
            ready.append((h.stem, got[1]))
    queue = list(ready)
    running: list[tuple[str, subprocess.Popen, Path]] = []
    while queue or running:
        while queue and len(running) < max(1, a.jobs):
            name, args = queue.pop(0)
            log = bdir / f"{name}.log"
            with open(log, "w") as f:
                p = subprocess.Popen(args + [f"-max_total_time={a.secs}"], stdout=f, stderr=subprocess.STDOUT)
            running.append((name, p, log))
        time.sleep(0.2)
        for item in list(running):
            name, p, log = item
            if p.poll() is not None:
                running.remove(item)
                good, msg = verdict(name, p.returncode, log, a.secs)
                ok &= good
                print(msg, flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
