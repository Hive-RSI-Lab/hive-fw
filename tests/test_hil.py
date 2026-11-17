import json
import os
import tempfile
import unittest
from pathlib import Path

from tests import _util  # noqa: F401
import hil


class FakeBoard:
    """A remote board behind the bench: volume/mute state, a diag line, and the bench's own refusals."""

    def __init__(self, reboot=False, heap_min=12379, freeze=False, lease=True):
        self.vol, self.mute, self.up = 35, False, 100
        self.reboot, self.heap_min, self.freeze, self.lease = reboot, heap_min, freeze, lease
        self.sent, self.diags = [], 0

    def call(self, cmd, timeout=0):
        self.sent.append(cmd)
        self.up += 1
        op, _, body = cmd[4:].partition(" ")
        if op == "ota":
            return {"error": "`api ota` goes over USB only: over Wi-Fi it could strand the board", "status": -1}
        if body and not self.lease:
            return {"error": "Lite 5 is held by lane x until 12:00", "status": -1}
        if op == "diag":
            self.diags += 1
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
            self.assertEqual(hil.main(["--plan", str(self.write(PLAN)), "--dev", "x"]), 3)
        finally:
            os.environ.clear()
            os.environ.update(env)


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

    def test_lease_refusal_is_a_failure_not_a_skip(self):
        res = hil.run(FakeBoard(lease=False), PLAN, {"bad"})
        self.assertEqual([n for n, _ in bad(res)], ["volume_101"])


if __name__ == "__main__":
    unittest.main()
