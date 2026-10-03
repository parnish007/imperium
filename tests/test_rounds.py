"""Stage 4: rounds, claims, escalation, trusted checks on snapshots, decisions; the builder's MCP tool.

The builder is simulated: the fake OpenCode server runs each prompt (an assistant reply), and the test edits the
workspace and calls the builder's tool the way the agent would. The workspace is a real git repository.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

from fake_opencode import FakeOpenCode
from helpers import TempHome
from imperium import cli, client, mcp, snapshot

SID = "ses_r1"
CONFIG = ("[opencode]\npoll_interval = 3600.0\n"
          "[delivery]\nidle_stable_polls = 1\nreconcile_window = 60.0\nadmit_timeout = 120.0\n"
          "[verification]\nbackend = 'unsafe-local'\n")
PY = sys.executable
TEST_SCRIPT = "import sys\nsys.path.insert(0, '.')\nfrom calc import add\nsys.exit(0 if add(2, 3) == 5 else 1)\n"


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def run(home, *args, env=None):
    out, err = io.StringIO(), io.StringIO()
    rc = cli.main(["--home", home, "--json"] + list(args), out=out, err=err, env=env or {})
    return rc, (json.loads(out.getvalue()) if out.getvalue().strip() else None)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.join(self.tmp.name, "ws")
        os.makedirs(os.path.join(self.ws, "tests"))
        self.write("calc.py", "def add(a, b):\n    return 0\n")
        self.write("tests/test_calc.py", TEST_SCRIPT)
        git(self.ws, "init", "-q")
        git(self.ws, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
        git(self.ws, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        self.fake = FakeOpenCode()
        self.fake.add_session(SID, self.ws)
        self.h = TempHome(CONFIG).init().start()
        self.now = 1_000_000.0
        self.h.d.engine.clock = lambda: self.now
        rc, body = run(self.h.home, "builder", "add", "coding", "--endpoint", self.fake.url, "--session", SID,
                       "--directory", self.ws)
        self.assertEqual(rc, 0, body)
        self.c = self.h.client()
        self.b = client.Client(self.h.home, principal="builder:coding")
        self.cycle()

    def tearDown(self):
        self.h.cleanup()
        self.fake.close()
        self.tmp.cleanup()

    def write(self, rel, text):
        path = os.path.join(self.ws, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def cycle(self, n=1):
        for _ in range(n):
            self.h.d.engine.run_once()

    def types(self):
        with self.h.d.store.read() as conn:
            return [r[0] for r in conn.execute("SELECT type FROM events ORDER BY seq")]

    def round(self, rid):
        return self.c.call("GET", f"/v1/round?id={rid}")["round"]

    def until(self, rid, state, cycles=6):
        for _ in range(cycles):
            if self.round(rid)["state"] == state:
                return
            self.cycle()
        self.assertEqual(self.round(rid)["state"], state)

    def standing_check(self, must_fail=True, depends=("tests/test_calc.py",)):
        return self.c.call("POST", "/v1/checks", {"id": "unit", "builder": "coding", "argv": [PY, "tests/test_calc.py"],
                                                  "depends": list(depends), "must_fail_on_base": must_fail,
                                                  "timeout": 60})["check"]

    def open(self, objective="make add() add", key="k1"):
        r = self.c.call("POST", "/v1/rounds", {"builder": "coding", "objective": objective, "client_key": key})["round"]
        self.until(r["id"], "OPEN")
        mine = [x for x in self.b.call("GET", "/v1/builder/rounds")["rounds"] if x["round"] == r["id"]]
        return r["id"], mine[0]["nonce"]

    def report(self, rid, nonce, gen=1, state="ready", **kw):
        self.now += 11
        return self.b.call("POST", "/v1/builder/report", {"round": rid, "nonce": nonce, "generation": gen,
                                                          "state": state, **kw})

    def verify(self, rid):
        self.c.call("POST", "/v1/rounds/verify", {"id": rid})
        self.assertTrue(self.h.d.verifier.wait_idle(120))
        return self.round(rid)

    def fix(self):
        self.write("calc.py", "def add(a, b):\n    return a + b\n")


class TestSlice(Base):
    def test_one_round_end_to_end(self):
        self.standing_check()
        rid, nonce = self.open()
        msg = self.c.call("GET", f"/v1/round?id={rid}")["messages"][0]
        text = self.fake.find(SID, msg["oc_message_id"])["parts"][0]["text"]
        self.assertIn(f"round={rid} gen=1]", text.splitlines()[0])
        self.assertIn("make add() add", text)
        self.assertIn("escalate", text)
        self.assertIn(nonce, text)
        with open(os.path.join(self.ws, ".imperium", "rounds", f"{rid}.md"), encoding="utf-8") as f:
            self.assertIn(nonce, f.read())
        self.fix()
        r = self.report(rid, nonce, gates=["python tests/test_calc.py"])
        self.assertTrue(r["recorded"])
        self.assertEqual(self.round(rid)["state"], "CLAIMED_READY")
        r = self.verify(rid)
        self.assertEqual(r["checks_ok_generation"], 1)
        self.assertEqual(r["state"], "CLAIMED_READY")  # evidence, but the objective is not yet judged
        self.c.call("POST", "/v1/rounds/objective", {"id": rid, "met": True, "note": "add works"})
        self.assertEqual(self.round(rid)["state"], "VERIFIED")
        self.c.call("POST", "/v1/rounds/decide", {"id": rid, "decision": "accept", "note": "good"})
        self.assertEqual(self.round(rid)["state"], "ACCEPTED")
        runs = self.c.call("GET", f"/v1/round?id={rid}")["check_runs"]
        self.assertEqual({(x["target"], x["exit_code"]) for x in runs}, {("candidate", 0), ("base", 1)})
        cand = [x for x in runs if x["target"] == "candidate"][0]
        self.assertEqual(cand["tree"], self.round(rid)["cand_tree"])
        self.assertTrue(cand["executable_sha256"])
        for t in ("ROUND_OPENED", "ROUND_STARTED", "SNAPSHOT_TAKEN", "CLAIM_READY", "CHECKS_PASSED", "ROUND_VERIFIED",
                  "ROUND_ACCEPTED"):
            self.assertIn(t, self.types())

    def test_snapshot_includes_unstaged_and_untracked_and_leaves_the_index_alone(self):
        self.write("calc.py", "def add(a, b):\n    return a + b  # changed\n")
        self.write("new_file.py", "x = 1\n")
        self.write(".imperium/claims/zzz.json", "{}")
        before = git(self.ws, "status", "--porcelain")
        snap = snapshot.take(self.ws, "refs/imperium/test/snap")
        self.assertEqual(git(self.ws, "status", "--porcelain"), before)
        files = git(self.ws, "ls-tree", "-r", "--name-only", snap["commit"]).splitlines()
        self.assertIn("new_file.py", files)
        self.assertFalse(any(f.startswith(".imperium") for f in files))
        self.assertIn("# changed", git(self.ws, "show", f"{snap['commit']}:calc.py"))


class TestClaims(Base):
    def test_wrong_nonce_and_stale_generation_change_nothing(self):
        rid, nonce = self.open()
        with self.assertRaises(client.ApiError) as e:
            self.report(rid, "not-the-nonce")
        self.assertEqual(e.exception.status, 400)
        self.assertIn("CLAIM_REJECTED", self.types())  # the rejection is journaled despite the refusal
        r = self.report(rid, nonce, gen=7)
        self.assertFalse(r["recorded"])
        self.assertIn("STALE_CLAIM", self.types())
        self.assertEqual(self.round(rid)["state"], "OPEN")

    def test_duplicate_and_too_frequent_reports(self):
        rid, nonce = self.open()
        self.report(rid, nonce, state="incomplete", not_done=["x"])
        self.assertFalse(self.report(rid, nonce, state="incomplete", not_done=["x"])["recorded"])
        self.report(rid, nonce, state="incomplete", not_done=["y"])
        with self.assertRaises(client.ApiError) as e:  # a different report within 10 s of the last one
            self.b.call("POST", "/v1/builder/report", {"round": rid, "nonce": nonce, "generation": 1,
                                                       "state": "ready"})
        self.assertEqual(e.exception.status, 429)

    def test_claim_file_channel(self):
        rid, nonce = self.open()
        self.write(f".imperium/claims/{rid}.json", json.dumps({"round": rid, "nonce": nonce, "generation": 1,
                                                               "state": "ready"}))
        self.cycle()
        self.assertEqual(self.round(rid)["state"], "CLAIMED_READY")
        self.write(f".imperium/claims/{rid}.json", "{not json")
        self.cycle()
        self.assertIn("CLAIM_REJECTED", self.types())

    def test_escalation_is_a_flag_answered_by_a_repair_message(self):
        rid, nonce = self.open()
        self.b.call("POST", "/v1/builder/escalate", {"round": rid, "nonce": nonce, "issue_type": "blocked",
                                                     "problem_assessment": "the test needs a network"})
        self.assertEqual(self.round(rid)["escalation"], "open")
        self.assertIn("ESCALATED", self.types())
        self.c.call("POST", "/v1/rounds/message", {"id": rid, "body": "skip the network part", "client_key": "m1"})
        self.cycle(3)
        r = self.round(rid)
        self.assertEqual((r["escalation"], r["generation"], r["state"]), ("answered", 2, "OPEN"))

    def test_repair_starts_a_new_generation_and_voids_verified(self):
        self.standing_check()
        rid, nonce = self.open()
        self.fix()
        self.report(rid, nonce)
        self.verify(rid)
        self.c.call("POST", "/v1/rounds/objective", {"id": rid, "met": True})
        self.assertEqual(self.round(rid)["state"], "VERIFIED")
        self.c.call("POST", "/v1/rounds/message", {"id": rid, "body": "also handle floats", "client_key": "m1"})
        self.cycle(3)
        r = self.round(rid)
        self.assertEqual((r["state"], r["generation"]), ("OPEN", 2))
        self.assertFalse(self.report(rid, nonce, gen=1)["recorded"])  # stale
        text = self.fake.messages[SID][-2]["parts"][0]["text"]
        self.assertIn("gen=2]", text.splitlines()[0])
        self.assertIn("generation=2", text)
        with self.assertRaises(client.ApiError):
            self.c.call("POST", "/v1/rounds/decide", {"id": rid, "decision": "accept"})


class TestTrustedChecks(Base):
    def test_builder_weakening_the_test_makes_the_check_untrusted(self):
        self.standing_check()
        rid, nonce = self.open()
        self.write("tests/test_calc.py", "import sys\nsys.exit(0)\n")  # the cheat: the test always passes
        self.report(rid, nonce)
        r = self.verify(rid)
        self.assertTrue(r["untrusted"])
        self.assertIsNone(r["checks_ok_generation"])
        self.assertIn("UNTRUSTED_CHECKS", self.types())
        self.assertIn("TEST_FILES_CHANGED", self.types())
        self.c.call("POST", "/v1/rounds/objective", {"id": rid, "met": True})
        self.assertEqual(self.round(rid)["state"], "CLAIMED_READY")

    def test_only_the_owner_approves_changed_check_files(self):
        from imperium import tokens
        self.standing_check(must_fail=False)
        rid, nonce = self.open()
        self.fix()
        self.write("tests/test_calc.py", TEST_SCRIPT + "# a legitimate extra case\n")
        with self.h.d.store.tx() as conn:
            raw = tokens.issue(conn, "director:s1")
        tokens.write_locator(self.h.home, "director:s1", raw)
        director = client.Client(self.h.home, env={"CLAUDE_CODE_SESSION_ID": "s1"})
        with self.assertRaises(client.ApiError) as e:
            director.call("POST", "/v1/checks/approve", {"id": "unit"})
        self.assertEqual(e.exception.status, 403)
        with self.assertRaises(client.ApiError) as e:  # nor redefine it
            director.call("POST", "/v1/checks", {"id": "unit", "builder": "coding", "argv": [PY, "-c", "pass"]})
        self.assertEqual(e.exception.status, 403)
        self.c.call("POST", "/v1/checks/approve", {"id": "unit"})
        self.report(rid, nonce)
        r = self.verify(rid)
        self.assertFalse(r["untrusted"])
        self.assertEqual(r["checks_ok_generation"], 1)

    def test_a_check_that_passes_without_the_change_does_not_verify(self):
        self.c.call("POST", "/v1/checks", {"id": "trivial", "builder": "coding", "argv": [PY, "-c", "pass"],
                                           "must_fail_on_base": True})
        rid, nonce = self.open()
        self.fix()
        self.report(rid, nonce)
        r = self.verify(rid)
        self.assertIsNone(r["checks_ok_generation"])
        self.assertIn("GATE_NOT_DISCRIMINATING", self.types())

    def test_failing_check(self):
        self.standing_check()
        rid, nonce = self.open()
        self.report(rid, nonce)  # claims done without fixing
        r = self.verify(rid)
        self.assertEqual(r["state"], "CLAIMED_READY")
        self.assertIn("VERIFY_FAILED", self.types())

    def test_timeout_kills_the_check(self):
        self.c.call("POST", "/v1/checks", {"id": "slow", "builder": "coding",
                                           "argv": [PY, "-c", "import time; time.sleep(60)"], "timeout": 1})
        rid, nonce = self.open()
        self.report(rid, nonce)
        self.verify(rid)
        runs = self.c.call("GET", f"/v1/round?id={rid}")["check_runs"]
        self.assertEqual(runs[0]["timed_out"], 1)
        self.assertLess(runs[0]["duration"], 30)

    def test_check_runs_in_a_copy_not_the_workspace(self):
        self.c.call("POST", "/v1/checks", {"id": "writer", "builder": "coding",
                                           "argv": [PY, "-c", "open('written_by_check.txt','w').write('x')"]})
        rid, nonce = self.open()
        self.report(rid, nonce)
        self.verify(rid)
        self.assertFalse(os.path.exists(os.path.join(self.ws, "written_by_check.txt")))

    def test_no_checks_means_no_verification(self):
        rid, nonce = self.open()
        self.fix()
        self.report(rid, nonce)
        r = self.verify(rid)
        self.assertIsNone(r["checks_ok_generation"])


class TestDecisions(Base):
    def verified(self):
        self.standing_check()
        rid, nonce = self.open()
        self.fix()
        self.report(rid, nonce)
        self.verify(rid)
        self.c.call("POST", "/v1/rounds/objective", {"id": rid, "met": True})
        self.assertEqual(self.round(rid)["state"], "VERIFIED")
        return rid

    def test_accept_refused_if_the_workspace_changed_and_owner_override_is_journaled(self):
        rid = self.verified()
        self.write("calc.py", "def add(a, b):\n    return a - b\n")
        with self.assertRaises(client.ApiError) as e:
            self.c.call("POST", "/v1/rounds/decide", {"id": rid, "decision": "accept"})
        self.assertEqual(e.exception.status, 409)
        self.c.call("POST", "/v1/rounds/decide", {"id": rid, "decision": "accept", "override": True})
        r = self.round(rid)
        self.assertEqual((r["state"], r["override"]), ("ACCEPTED", 1))

    def test_first_decision_wins_and_queued_round_messages_are_cancelled(self):
        rid = self.verified()
        self.fake.set_status(SID, "busy")
        self.cycle()
        m = self.c.call("POST", "/v1/rounds/message", {"id": rid, "body": "one more thing", "client_key": "m"})
        self.c.call("POST", "/v1/rounds/decide", {"id": rid, "decision": "reject", "note": "no"})
        with self.assertRaises(client.ApiError) as e:
            self.c.call("POST", "/v1/rounds/decide", {"id": rid, "decision": "accept"})
        self.assertEqual(e.exception.status, 409)
        self.assertEqual(self.c.call("GET", f"/v1/message?id={m['message']['id']}")["message"]["state"], "CANCELLED")

    def test_objective_not_met_voids_verified(self):
        rid = self.verified()
        self.c.call("POST", "/v1/rounds/objective", {"id": rid, "met": False, "note": "floats missing"})
        self.assertEqual(self.round(rid)["state"], "CLAIMED_READY")

    def test_cancelled_opening_message_abandons_the_round(self):
        self.fake.set_status(SID, "busy")
        self.cycle()
        r = self.c.call("POST", "/v1/rounds", {"builder": "coding", "objective": "x", "client_key": "k"})["round"]
        msg = self.c.call("GET", f"/v1/round?id={r['id']}")["messages"][0]
        self.c.call("POST", "/v1/cancel", {"id": msg["id"]})
        self.assertEqual(self.round(r["id"])["state"], "ABANDONED")

    def test_open_is_idempotent_by_key(self):
        a = self.c.call("POST", "/v1/rounds", {"builder": "coding", "objective": "x", "client_key": "k"})
        b = self.c.call("POST", "/v1/rounds", {"builder": "coding", "objective": "x", "client_key": "k"})
        self.assertEqual(a["round"]["id"], b["round"]["id"])
        self.assertFalse(b["created"])
        with self.assertRaises(client.ApiError) as e:
            self.c.call("POST", "/v1/rounds", {"builder": "coding", "objective": "y", "client_key": "k"})
        self.assertEqual(e.exception.status, 409)


class TestBoundaries(Base):
    def test_builder_token_reaches_only_builder_routes(self):
        for method, path in (("GET", "/v1/status"), ("POST", "/v1/send"), ("POST", "/v1/rounds/decide"),
                             ("POST", "/v1/checks"), ("POST", "/v1/events_since")):
            with self.assertRaises(client.ApiError) as e:
                self.b.call(method, path, {})
            self.assertEqual(e.exception.status, 403, path)
        with self.assertRaises(client.ApiError) as e:
            self.c.call("POST", "/v1/builder/report", {})
        self.assertEqual(e.exception.status, 403)

    def test_builder_cannot_report_on_another_builders_round(self):
        rid, nonce = self.open()
        other = os.path.join(self.tmp.name, "other")
        os.makedirs(other)
        self.fake.add_session("ses_o", other)
        rc, _ = run(self.h.home, "builder", "add", "second", "--endpoint", self.fake.url, "--session", "ses_o",
                    "--directory", other)
        self.assertEqual(rc, 0)
        b2 = client.Client(self.h.home, principal="builder:second")
        with self.assertRaises(client.ApiError) as e:
            b2.call("POST", "/v1/builder/report", {"round": rid, "nonce": nonce, "generation": 1, "state": "ready"})
        self.assertEqual(e.exception.status, 400)


class TestBuilderMcp(Base):
    def test_tools_over_stdio(self):
        rid, nonce = self.open()
        server = mcp.builder_server(self.b)
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18",
                                                                           "capabilities": {}, "clientInfo": {}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "imperium_rounds", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                "name": "report_status", "arguments": {"round": rid, "nonce": nonce, "generation": 1,
                                                       "state": "ready"}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {
                "name": "report_status", "arguments": {"round": rid, "nonce": "bad", "generation": 1,
                                                       "state": "ready"}}},
        ]
        out = io.StringIO()
        server.serve(io.StringIO("\n".join(json.dumps(x) for x in lines) + "\n"), out)
        replies = {r["id"]: r for r in map(json.loads, out.getvalue().splitlines())}
        self.assertEqual(set(replies), {1, 2, 3, 4, 5})
        self.assertEqual(replies[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual({t["name"] for t in replies[2]["result"]["tools"]},
                         {"imperium_rounds", "report_status", "escalate"})
        self.assertIn(nonce, replies[3]["result"]["content"][0]["text"])
        self.assertFalse(replies[4]["result"]["isError"])
        self.assertTrue(replies[5]["result"]["isError"])
        self.assertEqual(self.round(rid)["state"], "CLAIMED_READY")

    def test_mcp_config_points_at_this_home(self):
        rc, body = run(self.h.home, "builder", "mcp-config", "coding")
        self.assertEqual(rc, 0, body)
        cmd = body["config"]["mcp"]["imperium"]["command"]
        self.assertEqual(cmd[-3:], ["builder-mcp", "--builder", "coding"])
        self.assertIn(self.h.home, cmd)


class TestCli(Base):
    def test_round_and_check_commands(self):
        rc, body = run(self.h.home, "check", "add", "unit", "--builder", "coding", "--depends", "tests/test_calc.py",
                       "--must-fail-on-base", "--", PY, "tests/test_calc.py")
        self.assertEqual(rc, 0, body)
        rc, body = run(self.h.home, "round", "open", "coding", "--objective", "fix add", "--key", "c1")
        self.assertEqual(rc, 0, body)
        rid = body["round"]["id"]
        self.cycle(3)
        nonce = self.b.call("GET", "/v1/builder/rounds")["rounds"][0]["nonce"]
        self.fix()
        self.report(rid, nonce)
        rc, body = run(self.h.home, "round", "verify", rid, "--wait", "60")
        self.assertEqual(rc, 0, body)
        rc, body = run(self.h.home, "round", "objective", rid, "met")
        self.assertEqual(body["round"]["state"], "VERIFIED")
        rc, body = run(self.h.home, "round", "diff", rid)
        self.assertEqual(rc, 0, body)
        self.assertIn("calc.py", [f["path"] for f in body["files"]])
        rc, body = run(self.h.home, "round", "accept", rid, "--note", "ok")
        self.assertEqual(body["round"]["state"], "ACCEPTED")


if __name__ == "__main__":
    unittest.main()
