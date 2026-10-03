"""The ACP builder adapter against a fake agent run as a real child process (tests/fake_acp.py)."""
import io
import json
import os
import sys
import tempfile
import time
import unittest

from helpers import TempHome
from imperium import acp, cli, client

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = ("[opencode]\npoll_interval = 3600.0\n"
          "[delivery]\nidle_stable_polls = 1\nreconcile_window = 60.0\nadmit_timeout = 120.0\n"
          "[verification]\nbackend = 'unsafe-local'\n")


def run(home, *args, env=None):
    out, err = io.StringIO(), io.StringIO()
    rc = cli.main(["--home", home, "--json"] + list(args), out=out, err=err, env=env or {})
    return rc, (json.loads(out.getvalue()) if out.getvalue().strip() else None)


class Base(unittest.TestCase):
    loadable = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.ws)
        self.state = os.path.join(self.tmp.name, "agent.json")
        if not self.loadable:
            os.environ["NOLOAD"] = "1"
            self.addCleanup(os.environ.pop, "NOLOAD", None)
        self.h = TempHome(CONFIG).init().start()
        self.now = 1_000_000.0
        self.h.d.engine.clock = lambda: self.now
        cmd = json.dumps([sys.executable, os.path.join(HERE, "fake_acp.py"), self.state])
        rc, body = run(self.h.home, "builder", "add", "agent", "--acp", cmd, "--directory", self.ws)
        self.assertEqual(rc, 0, body)
        self.c = self.h.client()
        self.until(lambda: self.cp().get("attached"))

    def tearDown(self):
        self.h.cleanup()
        self.tmp.cleanup()

    def cycle(self):
        self.h.d.engine.run_once()

    def until(self, cond, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.cycle()
            if cond():
                return
            time.sleep(0.05)
        self.fail("condition not reached")

    def cp(self):
        with self.h.d.store.read() as conn:
            r = conn.execute("SELECT data FROM checkpoints WHERE builder='agent'").fetchone()
        return json.loads(r[0]) if r else {}

    def agent(self):
        with open(self.state, encoding="utf-8") as f:
            return json.load(f)

    def send(self, body, key):
        return self.c.call("POST", "/v1/send", {"builder": "agent", "body": body, "client_key": key})["message"]

    def msg(self, mid):
        return self.c.call("GET", f"/v1/message?id={mid}")["message"]

    def types(self):
        with self.h.d.store.read() as conn:
            return [r[0] for r in conn.execute("SELECT type FROM events ORDER BY seq")]

    def builder(self):
        return self.c.call("GET", "/v1/builders?name=agent")["builder"]


class TestAcp(Base):
    def test_abort_all_cancels_active_turn_and_reports_confirmation(self):
        self.send('WAIT_CANCEL', 'cancel-me')
        self.until(lambda: self.cp().get('status') == 'busy')
        self.c.call('POST', '/v1/abort-all', {})
        self.until(lambda: self.c.call('GET', '/v1/status')['cancellations'][0]['state'] == 'confirmed')
        self.assertEqual(self.cp()['status'], 'idle')
        self.c.call('POST', '/v1/resume-all', {})

    def test_session_is_created_with_the_builder_tool_and_recorded(self):
        b = self.builder()
        self.assertTrue(b["session_id"].startswith("sess_"))
        new = [e for e in self.agent()["log"] if e.get("method") == "session/new"][0]["params"]
        self.assertEqual(new["cwd"], os.path.abspath(self.ws))
        self.assertEqual(new["mcpServers"][0]["name"], "imperium")
        self.assertIn("builder-mcp", new["mcpServers"][0]["args"])
        init = [e for e in self.agent()["log"] if e.get("method") == "initialize"][0]["params"]
        self.assertFalse(init["clientCapabilities"]["terminal"])  # Imperium offers no tools of its own

    def test_a_message_is_delivered_runs_and_carries_the_header(self):
        m = self.send("hello", "k1")
        self.until(lambda: self.msg(m["id"])["state"] == "ADMITTED")
        prompt = [e for e in self.agent()["log"] if e.get("method") == "session/prompt"][0]["params"]
        self.assertTrue(prompt["prompt"][0]["text"].startswith(f"[imperium msg={m['id']} builder=agent]"))
        self.until(lambda: self.cp().get("status") == "idle")
        self.assertIn("TURN_ENDED", self.types())

    def test_one_at_a_time_and_queue_order(self):
        a = self.send("SLOW first", "k1")
        b = self.send("second", "k2")
        self.until(lambda: self.msg(b["id"])["state"] == "ADMITTED")
        with self.h.d.store.read() as conn:
            rows = conn.execute("SELECT seq, type, data FROM events ORDER BY seq").fetchall()
        oc_a = self.msg(a["id"])["oc_message_id"]
        ended_a = [s for s, t, d in rows if t == "TURN_ENDED" and oc_a in d]
        sent_b = [s for s, t, d in rows if t == "MSG_DISPATCHING" and b["id"] in d]
        self.assertLess(ended_a[0], sent_b[0])  # never two prompts in flight
        texts = [e["params"]["prompt"][0]["text"] for e in self.agent()["log"] if e.get("method") == "session/prompt"]
        self.assertIn("SLOW first", texts[0])

    def test_a_permission_request_becomes_an_approval_and_the_reply_selects_the_agents_option(self):
        m = self.send("ASK make", "k1")
        self.until(lambda: self.c.call("GET", "/v1/approvals")["approvals"])
        ask = self.c.call("GET", "/v1/approvals")["approvals"][0]
        self.assertEqual(ask["permission"], "bash")
        self.assertEqual(ask["patterns"], ["make"])
        self.assertEqual(self.cp()["permissions"], [ask["id"]])
        self.c.call("POST", "/v1/approvals/decide", {"id": ask["id"], "reply": "once"})
        self.until(lambda: self.msg(m["id"])["state"] == "ADMITTED" and self.cp()["status"] == "idle")
        outcomes = [e["permission_outcome"] for e in self.agent()["log"] if "permission_outcome" in e]
        self.assertEqual(outcomes, [{"outcome": {"outcome": "selected", "optionId": "yes"}}])

    def test_reject_selects_the_reject_option(self):
        self.send("ASK rm", "k1")
        self.until(lambda: self.c.call("GET", "/v1/approvals")["approvals"])
        ask = self.c.call("GET", "/v1/approvals")["approvals"][0]
        self.c.call("POST", "/v1/approvals/decide", {"id": ask["id"], "reply": "reject"})
        self.until(lambda: any("permission_outcome" in e for e in self.agent()["log"]))
        outcome = [e["permission_outcome"] for e in self.agent()["log"] if "permission_outcome" in e][0]
        self.assertEqual(outcome["outcome"]["optionId"], "no")

    def test_an_error_answer_before_any_activity_is_rejected(self):
        m = self.send("REFUSE this", "k1")
        self.until(lambda: self.msg(m["id"])["state"] == "REJECTED")

    def test_a_crash_before_any_activity_is_uncertain_and_found_late_by_replay(self):
        m = self.send("SILENT_CRASH now", "k1")
        self.until(lambda: "ACP_PROCESS_EXITED" in self.types())
        self.until(lambda: self.cp().get("attached"))  # restarted, session loaded (the fake saved nothing)
        self.now += 120
        self.until(lambda: self.msg(m["id"])["state"] == "UNCERTAIN")
        self.assertEqual(self.builder()["session_id"], "sess_1")  # the same session, loaded

    def test_updates_that_are_not_turn_activity_prove_nothing(self):
        m = self.send("NOISE_CRASH now", "k1")
        self.until(lambda: "ACP_PROCESS_EXITED" in self.types())
        self.now += 120
        self.until(lambda: self.msg(m["id"])["state"] == "UNCERTAIN")

    def test_the_adapter_itself_refuses_a_second_prompt_in_flight(self):
        conn = self.h.d.engine.acp["agent"]
        conn.prompt_async(conn.session_id, "msg_a", [{"type": "text", "text": "[imperium msg=a1 builder=agent]\nSLOW"}])
        from imperium import opencode
        with self.assertRaises(opencode.OCError) as e:
            conn.prompt_async(conn.session_id, "msg_b", [{"type": "text", "text": "[imperium msg=b1 builder=agent]\nx"}])
        self.assertEqual(e.exception.status, 409)

    def test_replayed_history_proves_delivery_after_a_restart(self):
        m = self.send("SLOW work", "k1")
        self.until(lambda: self.msg(m["id"])["state"] in ("POSTED", "DELIVERED", "ADMITTED"))
        # the agent dies before Imperium saw it run; its saved history shows the message and a reply
        conn = self.h.d.engine.acp["agent"]
        time.sleep(1.8)
        with self.h.d.store.tx() as c:  # forget what the live process showed, as after a daemon restart
            c.execute("UPDATE outbox SET state='UNKNOWN' WHERE id=?", (m["id"],))
        conn.proc.kill()
        conn.proc.wait()
        conn.seen.clear()
        self.until(lambda: self.msg(m["id"])["state"] == "ADMITTED")
        self.assertIn("ACP_HISTORY_TOKEN", self.types())

    def test_removing_the_builder_stops_its_agent(self):
        proc = self.h.d.engine.acp["agent"].proc
        self.c.call("POST", "/v1/builders/remove", {"name": "agent"})
        self.cycle()
        proc.wait(timeout=10)
        self.assertNotIn("agent", self.h.d.engine.acp)

    def test_a_round_end_to_end(self):
        self.c.call("POST", "/v1/checks", {"id": "unit", "builder": "agent",
                                           "argv": [sys.executable, "-c",
                                                    "import sys; sys.exit(0 if open('out.txt').read() == 'ok' "
                                                    "else 1)"], "timeout": 60})
        import subprocess
        for args in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
                                       "-m", "init"]):
            subprocess.run(["git", *args], cwd=self.ws, check=True, capture_output=True)
        r = self.c.call("POST", "/v1/rounds", {"builder": "agent", "objective": "WRITE out.txt ok",
                                               "client_key": "r1"})["round"]
        self.until(lambda: self.c.call("GET", f"/v1/round?id={r['id']}")["round"]["state"] == "OPEN")
        self.until(lambda: os.path.exists(os.path.join(self.ws, "out.txt")) and self.cp()["status"] == "idle")
        b = client.Client(self.h.home, principal="builder:agent")
        nonce = [x for x in b.call("GET", "/v1/builder/rounds")["rounds"] if x["round"] == r["id"]][0]["nonce"]
        self.now += 11
        b.call("POST", "/v1/builder/report", {"round": r["id"], "nonce": nonce, "generation": 1, "state": "ready"})
        self.c.call("POST", "/v1/rounds/objective", {"id": r["id"], "met": True})
        self.c.call("POST", "/v1/rounds/verify", {"id": r["id"]})
        self.assertTrue(self.h.d.verifier.wait_idle(60))
        self.assertEqual(self.c.call("GET", f"/v1/round?id={r['id']}")["round"]["state"], "VERIFIED")
        self.c.call("POST", "/v1/rounds/decide", {"id": r["id"], "decision": "accept"})


class TestAcpWithoutLoad(Base):
    loadable = False

    def test_an_agent_that_cannot_load_gets_a_new_session_and_says_so(self):
        self.h.d.engine.acp["agent"].proc.kill()
        self.until(lambda: "ACP_SESSION_LOST" in self.types())
        self.until(lambda: self.builder()["session_id"] == "sess_2")


class TestParse(unittest.TestCase):
    def test_commands(self):
        self.assertEqual(acp.parse_command('["opencode", "acp"]'), ["opencode", "acp"])
        self.assertEqual(acp.parse_command("opencode acp --cwd x"), ["opencode", "acp", "--cwd", "x"])
        with self.assertRaises(acp.AcpError):
            acp.parse_command("[]")

    def test_patterns(self):
        self.assertEqual(acp._patterns({"locations": [{"path": "/a/b.py"}], "rawInput": {"command": "x"}}),
                         ["/a/b.py"])
        self.assertEqual(acp._patterns({"rawInput": {"command": ["git", "status"]}}), ["git status"])
        self.assertEqual(acp._patterns({"title": "Thinking"}), ["Thinking"])


if __name__ == "__main__":
    unittest.main()
