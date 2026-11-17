import json
import os
import tempfile
import unittest
from pathlib import Path

from tests import _util  # noqa: F401
import hil


class FakeBoard:
    """A remote board behind the bench: volume/mute state, a diag line, and the bench's own refusals."""

    def __init__(self, reboot=False, heap_min=12379, freeze=False, lease=True, no_diag=False, dies_after=None):
        self.vol, self.mute, self.up = 35, False, 100
        self.reboot, self.heap_min, self.freeze, self.lease = reboot, heap_min, freeze, lease
        self.no_diag, self.dies_after = no_diag, dies_after
        self.sent, self.diags, self.cleared = [], 0, 0

    def clear(self):
        self.cleared += 1
        return "dropped 1"

    def call(self, cmd, timeout=0):
        self.sent.append(cmd)
        if self.dies_after is not None and len(self.sent) > self.dies_after:
            return None
        self.up += 1
        op, _, body = cmd[4:].partition(" ")
        if op == "ota":
            return {"error": "`api ota` goes over USB only: over Wi-Fi it could strand the board", "status": -1}
        if body and not self.lease:
            return {"error": "Lite 5 is held by lane x until 12:00", "status": -1}
        if op == "diag":
            self.diags += 1
            if self.no_diag:
                return None
            up = 1 if self.reboot and self.diags > 1 else self.up
            return {"uptime_s": up, "boots": 1, "resets": "restart", "heap": {"free": 13000, "min": self.heap_min}}
        if op == "state":
            return {"audio": {"volume": self.vol, "mute": self.mute}}
        if op == "control":
            if '\\"voldown\\"' in body and not self.freeze:
                self.vol -= 1
            return {"ok": True}
        if op == "volume":
            b = json.loads(json.loads(body))
            if not isinstance(b.get("volume", 0), int) or not 0 <= b.get("volume", 0) <= 100:
                return {"error": "volume", "status": 400}
            self.vol, self.mute = b.get("volume", self.vol), b.get("mute", self.mute)
            return {"audio": {"volume": self.vol, "mute": self.mute}}
        return {"error": "no such op", "status": 404}


PLAN = {
    "suites": {
        "read": [
            {"name": "state", "op": "state", "expect": {"has": ["audio.volume"]}, "save": {"vol": "audio.volume", "mute": "audio.mute"}},
            {"name": "ota", "op": "ota"},
            {"name": "unknown", "op": "no/such", "expect": "404"},
        ],
        "bad": [{"name": "volume_101", "op": "volume", "body": {"volume": 101}, "expect": "4xx"}],
        "controls": [
            {"name": "set_30", "op": "volume", "body": {"volume": 30}, "expect": {"eq": {"audio.volume": 30}}},
            {"name": "next", "op": "control", "body": {"action": "next"}, "expect": {"true": ["ok"]}},
            {"name": "voldown", "op": "control", "body": {"action": "voldown"}, "expect": {"true": ["ok"]}},
            {"name": "moved", "op": "state", "expect": {"ne": {"audio.volume": 30}}, "why": "controls frozen"},
            {"name": "restore", "op": "volume", "body": {"volume": "$vol", "mute": "$mute"}, "expect": {"eq": {"audio.volume": "$vol"}}},
        ],
    },
    "after": {"heap_min_floor": 10331},
}


def bad(res):
    return [(r["name"], r["why"]) for r in res if r["pass"] is False]


class Lines(unittest.TestCase):
    def test_line_quotes_json(self):
        self.assertEqual(hil.line("volume", {"volume": 4}), 'api volume "{\\"volume\\":4}"')
        self.assertEqual(hil.line("info"), "api info")
        self.assertEqual(hil.line("volume", "{"), 'api volume "{"')

    def test_subst_keeps_type_and_longest_name_first(self):
        self.assertEqual(hil.subst({"volume": "$vol"}, {"vol": 35}), {"volume": 35})
        self.assertEqual(hil.subst('{"v":$volume}', {"vol": 1, "volume": 2}), '{"v":2}')


class Judge(unittest.TestCase):
    def test_status_classes(self):
        self.assertEqual(hil.judge("4xx", {"status": 400}), "")
        self.assertIn("accepted", hil.judge("4xx", {"ok": True}))
        self.assertEqual(hil.judge("404", {"status": 404}), "")
        self.assertEqual(hil.judge("ok", None), "no answer")

    def test_refusals_skip_only_for_the_named_reasons(self):
        self.assertTrue(hil.judge("ok", {"error": "`api ota` goes over USB only", "status": -1}).startswith(hil.SKIP))
        self.assertTrue(hil.judge("4xx", {"error": "the body isn't JSON", "status": -1}).startswith(hil.SKIP))
        why = hil.judge("ok", {"error": "held by lane x", "status": -1})
        self.assertFalse(why.startswith(hil.SKIP))
        self.assertIn("held by lane x", why)

    def test_paths(self):
        a = {"audio": {"volume": 30, "mute": False}, "rows": [{"n": 1}]}
        self.assertEqual(hil.judge({"has": ["audio.volume", "rows.0.n"]}, a), "")
        self.assertIn("missing", hil.judge({"has": ["audio.gain"]}, a))
        self.assertIn("not true", hil.judge({"true": ["audio.mute"]}, a))
        self.assertEqual(hil.judge({"eq": {"audio.volume": "$v"}}, a, {"v": 30}), "")
        self.assertIn("want 31", hil.judge({"eq": {"audio.volume": 31}}, a))
        self.assertIn("still", hil.judge({"ne": {"audio.volume": 30}}, a))


class Plans(unittest.TestCase):
    def write(self, plan):
        d = tempfile.mkdtemp()
        p = Path(d) / "plan.json"
        p.write_text(json.dumps(plan))
        return p

    def test_good_plan(self):
        self.assertEqual(hil.load_plan(self.write(PLAN))[1], [])

    def test_bad_plans_say_what(self):
        self.assertTrue(hil.load_plan(self.write({}))[1])
        probs = hil.load_plan(self.write({"suites": {"s": [{"name": "x"}, {"name": "y", "op": "a", "expect": "5xx"},
                                                          {"name": "z", "op": "a", "expect": {"eqq": {}}}]},
                                          "after": {"heap_floor": 1}}))[1]
        self.assertEqual(len(probs), 4, probs)

    def test_list_sends_nothing(self):
        self.assertEqual(hil.main(["--plan", str(self.write(PLAN)), "--list"]), 0)
        self.assertEqual(hil.main(["--plan", str(self.write(PLAN)), "--list", "--suite", "nope"]), 2)

    def test_no_bench_exits_3(self):
        env = dict(os.environ)
        try:
            os.environ["PATH"], os.environ["FW_SERIALTAB"] = "/nonexistent", ""
            self.assertEqual(hil.main(["--plan", str(self.write(PLAN)), "--dev", "30:76:F5:2D:74:00"]), 3)
        finally:
            os.environ.clear()
            os.environ.update(env)

    def test_dev_must_be_an_id(self):
        self.assertEqual(hil.main(["--plan", str(self.write(PLAN)), "--dev", "Lite 5"]), 2)


class Benches(unittest.TestCase):
    """Bench.call against a fake serialtab: a line only queued, or dropped by the bench, stops at once."""

    def bench(self, script):
        d = Path(tempfile.mkdtemp())
        st = d / "serialtab"
        st.write_text("#!/bin/sh\n" + script)
        st.chmod(0o755)
        (d / "remote").mkdir()
        return hil.Bench("3076f52d7400", serialtab=str(st), root=d), d / "remote" / "3076f52d7400.log"

    def test_offline_board_is_a_stop(self):
        b, _ = self.bench("echo 'note: 3076f52d7400 is offline; it goes when the board dials in' >&2\n")
        a = b.call("api state", timeout=1)
        self.assertEqual(a["status"], hil.STOP)
        self.assertIn("only queued", hil.judge("ok", a))

    def test_dropped_line_is_a_stop(self):
        b, log = self.bench("")
        b.me = "a4b54"
        log.write_text("2026-11-17 05:00:00 --- a4b54's line 7 not sent: the board is offline\n")
        b._size = lambda: 0
        self.assertEqual(b.call("api state", timeout=1)["status"], hil.STOP)

    def test_lost_answer_stops_at_once(self):
        b, log = self.bench("")
        b.me = "a4b54"
        log.write_text("2026-11-17 05:00:00 > api state   (a4b54)\n2026-11-17 05:00:20 --- no answer to state: timed out\n")
        b._size = lambda: 0
        self.assertIn("timed out", b.call("api state", timeout=5)["error"])

    def test_answer_is_read_from_the_log(self):
        b, log = self.bench("")
        b.me = "a4b54"
        log.write_text('2026-11-17 05:00:00 > api state   (a4b54)\n2026-11-17 05:00:01 API {"audio": {"volume": 3}}\n')
        b._size = lambda: 0
        self.assertEqual(b.call("api state", timeout=1), {"audio": {"volume": 3}})


class Runs(unittest.TestCase):
    def test_healthy_board_passes_and_is_put_back(self):
        b = FakeBoard()
        res = hil.run(b, PLAN)
        self.assertEqual(bad(res), [])
        self.assertEqual([r["name"] for r in res if r.get("skip")], ["ota"])
        self.assertEqual(b.vol, 35)
        self.assertEqual(res[-1]["answer"]["heap_min_floor"], 10331)

    def test_frozen_controls_fail(self):
        res = hil.run(FakeBoard(freeze=True), PLAN, {"read", "controls"})
        self.assertEqual([n for n, _ in bad(res)], ["moved"])
        self.assertIn("controls frozen", bad(res)[0][1])

    def test_restart_fails_after(self):
        res = hil.run(FakeBoard(reboot=True), PLAN, {"read"})
        self.assertIn("restarted", res[-1]["why"])

    def test_heap_min_under_floor_fails(self):
        res = hil.run(FakeBoard(heap_min=9000), PLAN, {"read"})
        self.assertFalse(res[-1]["pass"])
        self.assertIn("under the floor 10331", res[-1]["why"])
        self.assertTrue(hil.run(FakeBoard(heap_min=10331), PLAN, {"read"})[-1]["pass"])

    def test_floor_from_the_command_line_wins(self):
        res = hil.run(FakeBoard(heap_min=12000), PLAN, {"read"}, heap_min_floor=12379)
        self.assertIn("under the floor 12379", res[-1]["why"])

    def test_no_heap_min_with_a_floor_fails(self):
        why = hil.after_check({"uptime": 1}, {"uptime": 2, "heap_min": None}, dict(hil.AFTER, heap_min_floor=1))
        self.assertIn("no least-ever-free heap", why)
        self.assertEqual(hil.after_check({"uptime": 1}, {"uptime": 2}, hil.AFTER), "")

    def test_no_diag_before_sends_nothing(self):
        b = FakeBoard(no_diag=True)
        res = hil.run(b, PLAN)
        self.assertEqual(b.sent, ["api diag"])
        self.assertEqual(len(res), 1)
        self.assertIn("no diag before the run", res[0]["why"])
        self.assertEqual(b.cleared, 1)

    def test_unsaved_var_is_not_sent(self):
        b = FakeBoard()
        res = hil.run(b, PLAN, {"controls"})
        self.assertEqual([n for n, _ in bad(res)], ["restore"])
        self.assertIn("$vol", bad(res)[0][1])
        self.assertFalse([c for c in b.sent if "$" in c])

    def test_silent_board_stops_the_run(self):
        b = FakeBoard(dies_after=3)
        res = hil.run(b, PLAN)
        self.assertEqual(len(b.sent), 4)
        self.assertTrue(all(r["pass"] is False for r in res[3:]))
        self.assertIn("not sent: the run stopped at read/unknown", res[4]["why"])
        self.assertIn("not checked", res[-1]["why"])
        self.assertEqual(b.cleared, 1)

    def test_failed_save_holds_back_the_rest_of_its_suite(self):
        plan = {"suites": {"c": [{"name": "save", "op": "state", "expect": {"has": ["audio.gain"]}, "save": {"vol": "audio.volume"}},
                                 {"name": "mute", "op": "volume", "body": {"mute": True}},
                                 {"name": "restore", "op": "volume", "body": {"volume": "$vol", "mute": False}}],
                           "d": [{"name": "state", "op": "state"}]}}
        b = FakeBoard()
        res = hil.run(b, plan)
        self.assertEqual([r["name"] for r in res if r["pass"] is False], ["save", "mute", "restore"])
        self.assertIn("didn't save vol", res[2]["why"])
        self.assertFalse(b.mute)
        self.assertEqual(b.sent, ["api diag", "api state", "api state", "api diag"])

    def test_a_save_path_missing_fails_the_save(self):
        plan = {"suites": {"c": [{"name": "save", "op": "state", "save": {"g": "audio.gain"}},
                                 {"name": "mute", "op": "volume", "body": {"mute": True}}]}}
        b = FakeBoard()
        res = hil.run(b, plan)
        self.assertIn("missing ['audio.gain'] to save", res[0]["why"])
        self.assertFalse(b.mute)

    def test_a_longer_name_is_not_filled_by_a_shorter(self):
        self.assertEqual(hil.subst('{"v":$volx}', {"vol": 1}), '{"v":$volx}')

    def test_restart_is_seen_against_wall_time(self):
        before = {"uptime": 100}
        self.assertEqual(hil.after_check(before, {"uptime": 400}, hil.AFTER, elapsed=300), "")
        self.assertIn("restarted", hil.after_check(before, {"uptime": 150}, hil.AFTER, elapsed=300))

    def test_cut_off_answer_fails(self):
        self.assertIn("unparsed", hil.judge("4xx", {"error": "unparsed: {", "status": -2}))

    def test_lease_refusal_is_a_failure_not_a_skip(self):
        res = hil.run(FakeBoard(lease=False), PLAN, {"bad"})
        self.assertEqual([n for n, _ in bad(res)], ["volume_101"])


if __name__ == "__main__":
    unittest.main()
