#!/usr/bin/env python3
"""fw hil: on-hardware suites for a remote board, over a Hive's Wi-Fi bench (serialtab).

A plan file (JSON) lists the cases. Each case is one console line, usually
`api OP` or `api OP "<json>"`, and what its answer must be. When the run
ends, the "after" check reads the board's diag line again. It fails if the
board restarted or its heap fell during the run, or if its least-ever-free
heap went below the plan's floor. That floor is what makes a later run
catch a slow leak rather than just record it.

  fw hil --dev D --plan PLAN.json [--suite a,b] [--lane L] [--json OUT]
         [--heap-min-floor BYTES] [--list]

Plan:
  {"suites": {"read": [CASE, ...], "bad": [...], ...},     run in this order
   "after": {"line": "api diag", "uptime": "uptime_s", "boots": "boots",
             "resets": "resets", "heap_free": "heap.free", "heap_min": "heap.min",
             "heap_drop": 4096, "heap_min_floor": 10331},
   "skip_refused": ["over USB only", "the body "]}

CASE: {"name": N, "op": "volume", "body": {...} or "raw text", "expect": E,
       "save": {"var": "path"}, "timeout": S, "why": "what a failure means"}
  or  {"name": N, "line": "any console line", ...}
  E is "ok" (status 200), "4xx" or "404", or an object with any of
  "status" ("ok" by default), "has": [paths], "true": [paths],
  "eq": {path: value}, "ne": {path: value}. A path is dotted (audio.volume).
  A value of "$var" in a body or in eq/ne is what an earlier case saved.

The bench refuses some lines before they reach the board, for example a
Wi-Fi update that could strand the board, or a body that isn't a JSON
object. A case refused with a reason in "skip_refused" counts as SKIP.
Any other refusal is a FAIL, such as a missing lease or the endpoint being
down. A board you don't hold refuses write lines, so take its lease first
(`serialtab lease take --dev D --why ...`).

The run stops sending when a line gets no answer, when the bench itself
fails, or when the board is offline or its agent switch is off: a queued
line would go later and its answer could be read as a later case's. The
cases left FAIL as not sent, and the run asks the bench to drop any of
your lines it still holds (`serialtab clear --dev D`). A case whose line
still holds a "$var" that no earlier case saved FAILs without being sent,
and when a case with "save" fails, the rest of its suite is not sent: the
writes after it could not be put back. The run starts with the
diag line; with no diag it sends nothing.

Exit 0 when every case that ran passed, 1 when one failed, 2 on a bad plan,
and 3 when there is no bench (serialtab is not on PATH or in $FW_SERIALTAB).
"""
import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPLY_S = 20
SKIP = "skip: "
NO_BENCH = 3
AFTER = {"line": "api diag", "uptime": "uptime_s", "boots": "boots", "resets": "resets",
         "heap_free": "heap.free", "heap_min": "heap.min", "heap_drop": 4096, "heap_min_floor": None}
SKIP_REFUSED = ["over USB only", "the body "]
STOP = -3                   # status of an answer after which nothing more is sent
QUEUED = ("is offline", "agent switch is off")   # serialtab send's notes for a line it only queued
SEND_S = 30
RESTART_SLACK_S = 30
UNFILLED = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\b")


def line(op, body=None):
    """The console line for op with an optional JSON body, quoted for the board's console."""
    if body is None:
        return f"api {op}"
    raw = body if isinstance(body, str) else json.dumps(body, separators=(",", ":"))
    return 'api %s "%s"' % (op, raw.replace("\\", "\\\\").replace('"', '\\"'))


_MISSING = object()


def get(a, path):
    for k in path.split(".") if path else []:
        if isinstance(a, dict) and k in a:
            a = a[k]
        elif isinstance(a, list) and k.isdigit() and int(k) < len(a):
            a = a[int(k)]
        else:
            return _MISSING
    return a


def subst(v, saved):
    """Put saved values in for "$var" strings (whole values keep their type; inside raw text, as JSON)."""
    if isinstance(v, str):
        if v.startswith("$") and v[1:] in saved:
            return saved[v[1:]]
        return UNFILLED.sub(lambda m: json.dumps(saved[m.group(0)[1:]]) if m.group(0)[1:] in saved else m.group(0), v)
    if isinstance(v, dict):
        return {k: subst(x, saved) for k, x in v.items()}
    if isinstance(v, list):
        return [subst(x, saved) for x in v]
    return v


def status(a):
    st = a.get("status") if isinstance(a, dict) else None
    return st if isinstance(st, int) and not isinstance(st, bool) else 200


def judge(expect, a, saved=None, skip_refused=SKIP_REFUSED):
    """'' when the answer is what the case expects, else why not (SKIP-prefixed for a line the bench won't send)."""
    if a is None:
        return "no answer"
    if isinstance(a, dict) and a.get("status") == -1:
        err = str(a.get("error"))
        if any(s in err for s in skip_refused):
            return SKIP + "the bench refused to send it (" + err[:90] + ")"
        return "the bench refused it: " + err[:200]
    if isinstance(a, dict) and a.get("status") in (-2, STOP):
        return str(a.get("error"))[:200]
    if not isinstance(a, dict):
        return f"answer is not an object: {str(a)[:80]}"
    e = expect if isinstance(expect, dict) else {"status": expect or "ok"}
    want, st = e.get("status", "ok"), status(a)
    if want == "ok" and st != 200:
        return f"status {st}: {a.get('error')}"
    if want == "4xx" and not 400 <= st < 500:
        return f"accepted (status {st})"
    if want == "404" and st != 404:
        return f"status {st}"
    e = subst(e, saved or {})
    miss = [p for p in e.get("has", []) if get(a, p) is _MISSING]
    if miss:
        return f"missing {miss}"
    falsy = [p for p in e.get("true", []) if not get(a, p) or get(a, p) is _MISSING]
    if falsy:
        return f"not true: {falsy}"
    for p, v in e.get("eq", {}).items():
        if get(a, p) != v:
            got = get(a, p)
            return f"{p} is {'missing' if got is _MISSING else json.dumps(got)}, want {json.dumps(v)}"
    for p, v in e.get("ne", {}).items():
        if get(a, p) == v:
            return f"{p} is still {json.dumps(v)}"
    return ""


def load_plan(path):
    """The plan, checked: (plan, problems)."""
    try:
        p = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        return None, [f"{path}: {e}"]
    bad = []
    suites = p.get("suites") if isinstance(p, dict) else None
    if not isinstance(suites, dict) or not suites:
        return None, [f"{path}: no \"suites\" object"]
    for s, cases in suites.items():
        for i, c in enumerate(cases if isinstance(cases, list) else [None]):
            where = f"{s}[{i}]"
            if not isinstance(c, dict) or not c.get("name"):
                bad.append(f"{where}: a case is an object with a \"name\"")
                continue
            if ("op" in c) == ("line" in c):
                bad.append(f"{where} {c['name']}: give \"op\" or \"line\", not both")
            e = c.get("expect", "ok")
            if isinstance(e, str) and e not in ("ok", "4xx", "404"):
                bad.append(f"{where} {c['name']}: expect {e!r} is not ok, 4xx or 404")
            if isinstance(e, dict) and set(e) - {"status", "has", "true", "eq", "ne"}:
                bad.append(f"{where} {c['name']}: unknown expect keys {sorted(set(e) - {'status', 'has', 'true', 'eq', 'ne'})}")
    after = p.get("after", {})
    if not isinstance(after, dict) or set(after) - set(AFTER):
        bad.append(f"after: unknown keys {sorted(set(after) - set(AFTER)) if isinstance(after, dict) else after}")
    return p, bad


class Bench:
    """One remote board over serialtab: send a line, then wait for its `API` answer in the board's log."""

    def __init__(self, dev, lane=None, serialtab=None, root=None):
        self.dev, self.lane = dev, lane
        self.serialtab = serialtab or os.environ.get("FW_SERIALTAB") or shutil.which("serialtab")
        root = Path(root or os.environ.get("H2_SERIAL_DIR") or Path.home() / ".h2" / "serial")
        self.log = root / "remote" / f"{dev}.log"
        self.me = (os.environ.get("H2_NAME") or os.environ.get("USER") or "agent")[:20]

    def _size(self):
        try:
            return self.log.stat().st_size
        except FileNotFoundError:
            return 0

    def call(self, cmd, timeout=REPLY_S):
        start = self._size()
        args = [self.serialtab, "send", "--dev", self.dev, cmd] + (["--lane", self.lane] if self.lane else [])
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=SEND_S)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"error": f"the bench didn't take the line: {e}"[:300], "status": STOP}
        if r.returncode:
            return {"error": (r.stderr or r.stdout).strip()[-300:], "status": -1}
        if any(q in r.stderr for q in QUEUED):
            return {"error": "only queued, not sent: " + r.stderr.strip()[-200:], "status": STOP}
        mark, gone, end = f"> {cmd}", f"--- {self.me}'s line ", time.time() + timeout
        while time.time() < end:
            try:
                with open(self.log, "rb") as f:
                    f.seek(start)
                    lines = f.read().decode("utf-8", "replace").splitlines()
            except FileNotFoundError:
                lines = []
            for i, ln in enumerate(lines):   # "<19-char stamp> > cmd   (agent)", then "<stamp> API {json}"
                if ln[20:].startswith(gone) and " not sent: " in ln:
                    return {"error": "the bench dropped it: " + ln[20:220], "status": STOP}
                if ln[20:].startswith(mark) and ln.rstrip().endswith(f"({self.me})"):
                    for nxt in lines[i + 1:]:
                        if nxt[20:].startswith("--- no answer to "):
                            return {"error": nxt[24:220], "status": STOP}
                        if nxt[20:].startswith("API "):
                            try:
                                return json.loads(nxt[24:])
                            except ValueError:
                                return {"error": "unparsed: " + nxt[24:200], "status": -2}
                        if nxt[20:].startswith("> "):
                            break
            time.sleep(0.5)
        return None

    def clear(self):
        """Drop this agent's lines the bench still holds for the board."""
        try:
            r = subprocess.run([self.serialtab, "clear", "--dev", self.dev], capture_output=True, text=True, timeout=SEND_S)
            return (r.stdout or r.stderr).strip()[-200:]
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"couldn't clear: {e}"


def snapshot(bench, after):
    d = bench.call(after["line"])
    d = d if isinstance(d, dict) else {}
    out = {}
    for k in ("uptime", "boots", "resets", "heap_free", "heap_min"):
        v = get(d, after[k])
        out[k] = None if v is _MISSING else v
    return out


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def after_check(before, now, after, elapsed=0):
    """Why the board is not as it was ('' when it held): a restart, a heap fall, the least-ever-free heap under the floor."""
    why = []
    if not _num(now.get("uptime")):
        return "no diag after the run"
    if _num(before.get("uptime")) and now["uptime"] < before["uptime"] + elapsed - RESTART_SLACK_S:
        why.append(f"restarted: uptime {before['uptime']} -> {now['uptime']} over {round(elapsed)} s")
    if before.get("boots") is not None and now.get("boots") != before.get("boots"):
        why.append(f"boots {before['boots']} -> {now['boots']}")
    if before.get("resets") and now.get("resets") != before.get("resets"):
        why.append(f"resets {before['resets']} -> {now['resets']}")
    if _num(now.get("heap_free")) and _num(before.get("heap_free")) and after.get("heap_drop") is not None \
            and now["heap_free"] < before["heap_free"] - after["heap_drop"]:
        why.append(f"heap fell {before['heap_free']} -> {now['heap_free']} (more than {after['heap_drop']} B)")
    floor = after.get("heap_min_floor")
    if floor is not None:
        if not _num(now.get("heap_min")):
            why.append(f"no least-ever-free heap ({after['heap_min']}) to hold to the floor {floor} B")
        elif now["heap_min"] < floor:
            why.append(f"least-ever-free heap {now['heap_min']} B is under the floor {floor} B")
    return "; ".join(why)


def run(bench, plan, suites=None, heap_min_floor=None):
    after = dict(AFTER, **plan.get("after", {}))
    if heap_min_floor is not None:
        after["heap_min_floor"] = heap_min_floor
    skip_refused = plan.get("skip_refused", SKIP_REFUSED)
    saved, out, stopped = {}, [], ""
    before = snapshot(bench, after)
    t_before = time.time()
    if not _num(before.get("uptime")):
        return [{"suite": "after", "name": "no_restart_heap_held", "pass": False, "answer": {"before": before},
                 "why": f"no diag before the run ({after['line']}), so nothing was sent"}]
    for suite, cases in plan["suites"].items():
        if suites and suite not in suites:
            continue
        unsaved = ""
        for c in cases:
            body = subst(copy.deepcopy(c.get("body")), saved)
            cmd = subst(c["line"], saved) if "line" in c else line(c["op"], body)
            r = {"suite": suite, "name": c["name"], "line": cmd[:160], "pass": False, "secs": 0, "answer": None}
            left = UNFILLED.findall(cmd)
            if stopped or unsaved or left:
                r["why"] = f"not sent: {stopped or unsaved}" if stopped or unsaved else \
                    f"not sent: {', '.join(left)} was never saved (run the case that saves it first)"
                out.append(r)
                continue
            t0 = time.time()
            a = bench.call(cmd, timeout=c.get("timeout", REPLY_S))
            secs = round(time.time() - t0, 1)
            if a is None or (isinstance(a, dict) and a.get("status") == STOP):
                stopped = f"the run stopped at {suite}/{c['name']}"
            why = judge(c.get("expect", "ok"), a, saved, skip_refused)
            if why and not why.startswith(SKIP) and c.get("why"):
                why = f"{c['why']}: {why}"
            if why and c.get("save"):
                unsaved = f"{suite}/{c['name']} didn't save {', '.join(c['save'])}, so nothing after it could be put back"
            if not why:
                for var, path in c.get("save", {}).items():
                    v = get(a, path)
                    if v is not _MISSING:
                        saved[var] = v
            r = {"suite": suite, "name": c["name"], "line": cmd[:160], "pass": not why, "why": why, "secs": secs, "answer": a}
            if why.startswith(SKIP):
                r["pass"], r["skip"] = None, True
            out.append(r)
    if stopped:
        dropped = bench.clear() if hasattr(bench, "clear") else ""
        out.append({"suite": "after", "name": "no_restart_heap_held", "pass": False, "answer": {"before": before, "clear": dropped},
                    "why": f"not checked: {stopped}" + (f" (bench: {dropped})" if dropped else "")})
        return out
    t_end = time.time()
    now = snapshot(bench, after)
    why = after_check(before, now, after, t_end - t_before)
    out.append({"suite": "after", "name": "no_restart_heap_held", "pass": not why, "why": why,
                "answer": {"before": before, "after": now, "heap_min_floor": after["heap_min_floor"]}})
    return out


def main(argv=None):
    p = argparse.ArgumentParser(prog="fw hil", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dev", help="the remote board's id: 12 hex digits, its MAC (colons or dashes are dropped)")
    p.add_argument("--plan", required=True, help="the plan JSON")
    p.add_argument("--suite", help="run only these suites, comma-separated (the plan's order holds)")
    p.add_argument("--lane", help="your lab lane, for the board's lease (serialtab's default otherwise)")
    p.add_argument("--json", help="write every case and answer here")
    p.add_argument("--heap-min-floor", type=int, help="fail when the least-ever-free heap is under this (overrides the plan)")
    p.add_argument("--list", action="store_true", help="print the plan's cases and lines, send nothing")
    a = p.parse_args(argv)
    plan, bad = load_plan(a.plan)
    if bad:
        print("fw hil: the plan is wrong:\n  " + "\n  ".join(bad), file=sys.stderr)
        return 2
    suites = set(a.suite.split(",")) if a.suite else None
    if suites and suites - set(plan["suites"]):
        print(f"fw hil: no suite {sorted(suites - set(plan['suites']))} in the plan (it has {', '.join(plan['suites'])})", file=sys.stderr)
        return 2
    if a.list:
        for s, cases in plan["suites"].items():
            if not suites or s in suites:
                for c in cases:
                    print(f"{s:9} {c['name']:26} {c['line'] if 'line' in c else line(c['op'], c.get('body'))}")
        return 0
    if not a.dev:
        p.error("--dev is needed to run (or --list)")
    dev = a.dev.lower().replace(":", "").replace("-", "")
    if not re.fullmatch(r"[0-9a-f]{12}", dev):
        print(f"fw hil: --dev {a.dev!r} is not a board id (12 hex digits, as `serialtab remote` lists)", file=sys.stderr)
        return 2
    bench = Bench(dev, a.lane)
    if not bench.serialtab:
        print("fw hil: no board bench here: serialtab isn't on PATH and $FW_SERIALTAB isn't set", file=sys.stderr)
        return NO_BENCH
    res = run(bench, plan, suites, a.heap_min_floor)
    for r in res:
        tag = "SKIP" if r.get("skip") else "PASS" if r["pass"] else "FAIL"
        print(f"{tag} {r['suite']:9} {r['name']:26} {r['why']}")
    n_skip = sum(bool(r.get("skip")) for r in res)
    n_bad = sum(r["pass"] is False for r in res)
    print(f"{len(res) - n_bad - n_skip}/{len(res) - n_skip} passed, {n_skip} skipped (the bench won't send them this way)")
    if a.json:
        Path(a.json).write_text(json.dumps({"dev": dev, "plan": str(a.plan), "t": time.strftime("%Y-%m-%dT%H:%M:%S"), "results": res}, indent=1))
    return 1 if n_bad else 0


if __name__ == "__main__":
    sys.exit(main())
