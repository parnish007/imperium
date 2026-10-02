"""Stages 6-9: the director's MCP server, watch, Claude Code hooks and plugin, liveness and the resource gate,
the read-only dashboard, export."""
import http.client
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from fake_opencode import FakeOpenCode
from helpers import TempHome
from imperium import cli, client, director_mcp, liveness

SID, DIR = "ses_f1", "/work/front"
CONFIG = ("[opencode]\npoll_interval = 3600.0\n[delivery]\nidle_stable_polls = 1\n"
          "[liveness]\nstall_after = 0.3\nmax_suppress = 0.6\n")
DIRECTOR_ENV = {"CLAUDE_CODE_SESSION_ID": "s1"}


def run(home, *args, env=None):
    out, err = io.StringIO(), io.StringIO()
    rc = cli.main(["--home", home, "--json"] + list(args), out=out, err=err, env=env or {})
    text = out.getvalue()
    try:
        return rc, json.loads(text) if text.strip() else None
    except ValueError:
        return rc, text


def raw(home, *args, env=None):
    out, err = io.StringIO(), io.StringIO()
    rc = cli.main(["--home", home] + list(args), out=out, err=err, env=env or {})
    return rc, out.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOpenCode()
        self.fake.add_session(SID, DIR)
        self.h = TempHome(CONFIG).init().start()
        rc, body = run(self.h.home, "builder", "add", "coding", "--endpoint", self.fake.url, "--session", SID,
                       "--directory", DIR)
        self.assertEqual(rc, 0, body)
        self.c = self.h.client()
        self.cycle()

    def tearDown(self):
        self.h.cleanup()
        self.fake.close()

    def cycle(self, n=1):
        for _ in range(n):
            self.h.d.engine.run_once()

    def types(self):
        with self.h.d.store.read() as conn:
            return [r[0] for r in conn.execute("SELECT type FROM events ORDER BY seq")]

    def claim(self):
        rc, body = run(self.h.home, "--as", "owner", "director", "claim", env=DIRECTOR_ENV)
        self.assertEqual(rc, 0, body)
        return client.Client(self.h.home, env=DIRECTOR_ENV)


class TestDirectorMcp(Base):
    def call(self, server, name, args, mid=1):
        reply = server.handle({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                               "params": {"name": name, "arguments": args}})
        return reply["result"]

    def test_tools_annotations_and_feed(self):
        d = self.claim()
        s = director_mcp.server(d)
        tools = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        by = {t["name"]: t for t in tools}
        self.assertTrue(by["events_since"]["annotations"]["readOnlyHint"])
        self.assertFalse(by["round_decide"]["annotations"]["readOnlyHint"])
        self.fake.add_user(SID, "typed by a human into the builder")
        self.cycle()
        r = self.call(s, "events_since", {})
        self.assertFalse(r["isError"])
        feed = json.loads(r["content"][0]["text"])
        self.assertIn("HUMAN_MESSAGE_SEEN", [e["headline"].split()[3] for e in feed["events"]])
        self.assertNotIn("typed by a human", r["content"][0]["text"])  # builder text only through `show`
        seq = feed["events"][-1]["seq"]
        self.assertFalse(self.call(s, "ack", {"seq": seq})["isError"])
        r = self.call(s, "round_open", {"builder": "coding", "objective": "x", "client_key": "k"})
        self.assertFalse(r["isError"])
        r = self.call(s, "decide_approval", {"id": "nope", "reply": "once"})
        self.assertTrue(r["isError"])

    def test_owner_only_actions_refused_through_the_director_tool(self):
        s = director_mcp.server(self.claim())
        r = self.call(s, "check_define", {"id": "c", "builder": "coding", "argv": ["x"]})
        self.assertFalse(r["isError"])
        r = self.call(s, "check_define", {"id": "c", "builder": "coding", "argv": ["y"]})
        self.assertTrue(r["isError"])
        self.assertIn("403", r["content"][0]["text"])


class TestWatchAndHooks(Base):
    def test_watch_prints_new_events_and_never_moves_the_bookmark(self):
        self.claim()
        self.fake.add_user(SID, "hello")
        self.cycle()
        rc, text = raw(self.h.home, "watch", "--consumer", "director", "--duration", "1", "--interval", "0.2",
                       env=DIRECTOR_ENV)
        self.assertEqual(rc, 0)
        self.assertIn("HUMAN_MESSAGE_SEEN", text)
        self.assertIn("WATCH_EXPIRING resume_after=", text)
        st = self.c.call("GET", "/v1/status")
        self.assertEqual([c["acked_seq"] for c in st["consumers"] if c["name"] == "director"], [0])

    def test_session_start_hook(self):
        rc, text = raw(self.h.home, "hook", "session-start", env=DIRECTOR_ENV)
        self.assertEqual(rc, 0)
        self.assertIn("not its director", text)
        self.claim()
        rc, text = raw(self.h.home, "hook", "session-start", env=DIRECTOR_ENV)
        self.assertIn("unacknowledged", text)

    def test_post_tool_use_hook_renews_presence_and_is_throttled(self):
        self.claim()
        self.assertFalse(self.h.d.lease_ok()[0])
        rc, text = raw(self.h.home, "hook", "post-tool-use", env=DIRECTOR_ENV)
        self.assertEqual((rc, text), (0, ""))
        self.assertTrue(self.h.d.lease_ok()[0])
        self.h.d.leases.clear()
        raw(self.h.home, "hook", "post-tool-use", env=DIRECTOR_ENV)
        self.assertFalse(self.h.d.lease_ok()[0])  # within 60 s: not sent again

    def test_hooks_never_fail(self):
        self.h.stop()
        for kind in ("session-start", "post-tool-use"):
            with mock.patch.object(cli, "cmd_up", side_effect=RuntimeError("no")):
                rc, _ = raw(self.h.home, "hook", kind, env=DIRECTOR_ENV)
            self.assertEqual(rc, 0)
        self.h.start()


class TestPlugin(unittest.TestCase):
    def test_generated_plugin(self):
        tmp = tempfile.mkdtemp()
        home = os.path.join(tmp, "home")
        rc, body = run(home, "plugin", "write", os.path.join(tmp, "plugin"))
        self.assertEqual(rc, 0, body)
        p = os.path.join(tmp, "plugin")
        manifest = json.load(open(os.path.join(p, ".claude-plugin", "plugin.json"), encoding="utf-8"))
        self.assertEqual(manifest["name"], "imperium")
        hooks = json.load(open(os.path.join(p, "hooks", "hooks.json"), encoding="utf-8"))
        self.assertIn("SessionStart", hooks["hooks"])
        self.assertIn("hook session-start", hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"])
        mcpcfg = json.load(open(os.path.join(p, ".mcp.json"), encoding="utf-8"))
        self.assertEqual(mcpcfg["mcpServers"]["imperium"]["command"], sys.executable)
        self.assertTrue(os.path.isfile(os.path.join(p, "skills", "imperium", "SKILL.md")))
        self.assertFalse(os.path.exists(os.path.join(p, "bin")))
        claude = shutil.which("claude")
        if claude:
            r = subprocess.run([claude, "plugin", "validate", p], capture_output=True, text=True, timeout=120)
            self.assertIn("Validation passed", r.stdout + r.stderr, r.stdout + r.stderr)
        shutil.rmtree(tmp, ignore_errors=True)


class TestLiveness(Base):
    def test_working_but_silent_is_reported_once(self):
        self.fake.set_status(SID, "busy")
        self.cycle()
        self.assertEqual(self.c.call("GET", "/v1/status")["builders"][0]["operational_state"], "WORKING")
        time.sleep(0.4)
        self.cycle(3)
        self.assertEqual(self.types().count("SUSPECTED_STALL"), 1)

    def test_waiting_is_not_stalled(self):
        self.fake.set_status(SID, "busy")
        self.fake.add_permission(SID)
        self.cycle()
        time.sleep(0.4)
        self.cycle(2)
        self.assertEqual(self.c.call("GET", "/v1/status")["builders"][0]["operational_state"], "WAITING_APPROVAL")
        self.assertNotIn("SUSPECTED_STALL", self.types())

    def test_busy_subagents_suppress_the_alarm_only_for_a_while(self):
        self.fake.add_session("ses_kid", DIR, parent=SID)
        self.fake.set_status("ses_kid", "busy")
        self.cycle()
        time.sleep(0.4)
        self.cycle()
        self.assertNotIn("SUSPECTED_STALL", self.types())
        time.sleep(0.3)
        self.cycle(2)
        self.assertEqual(self.types().count("HANG_SUSPECTED"), 1)

    def test_progress_resets_the_clock(self):
        self.fake.set_status(SID, "busy")
        a = self.fake.add_assistant(SID, parent_id="x", completed=False)
        self.cycle()
        for i in range(4):
            time.sleep(0.15)
            self.fake.find(SID, a["info"]["id"])["parts"][0]["text"] += " more"
            self.cycle()
        self.assertNotIn("SUSPECTED_STALL", self.types())


class TestGate(Base):
    def test_message_waits_for_resources(self):
        with mock.patch.object(liveness, "free_memory_gb", return_value=1.0):
            rc, body = run(self.h.home, "gate")
            self.assertEqual(rc, cli.REFUSED)
            self.assertEqual(body["gate"], "WAIT")
            m = self.c.call("POST", "/v1/send", {"builder": "coding", "body": "build it", "client_key": "k",
                                                 "needs_resources": True})["message"]
            self.cycle(2)
            self.assertEqual(self.c.call("GET", f"/v1/message?id={m['id']}")["message"]["state"], "QUEUED")
            self.assertIn("resources", self.c.call("GET", "/v1/status")["builders"][0]["dispatch_blocked"])
        with mock.patch.object(liveness, "free_memory_gb", return_value=8.0):
            self.cycle(2)
        self.assertEqual(self.c.call("GET", f"/v1/message?id={m['id']}")["message"]["state"], "ADMITTED")

    def test_free_memory_is_measured_here(self):
        self.assertIsNotNone(liveness.free_memory_gb())


class TestDashboard(Base):
    def get(self, path, token=None, host=None):
        port = self.h.d.port
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{port}"}
        if token:
            headers["Authorization"] = "Bearer " + token
        conn.request("GET", path, headers=headers)
        r = conn.getresponse()
        return r.status, dict(r.getheaders()), r.read()

    def test_static_page_and_read_only_token(self):
        status, headers, body = self.get("/dashboard")
        self.assertEqual(status, 200)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn(b"<script>", body)  # no inline script
        rc, body = run(self.h.home, "dashboard", "--no-open")
        self.assertEqual(rc, 0, body)
        token = body["url"].split("#token=")[1]
        status, _, data = self.get("/v1/status", token)
        self.assertEqual(status, 200, data)
        dash = client.Client(self.h.home)
        dash._cred = ("dashboard", token)
        with self.assertRaises(client.ApiError) as e:
            dash.call("POST", "/v1/stop-all", {})
        self.assertEqual(e.exception.status, 403)
        self.h.stop()
        self.h.start()
        status, _, _ = self.get("/v1/status", token)
        self.assertEqual(status, 401)

    def test_journal_read_does_not_move_feeds_and_hides_builder_text(self):
        self.fake.add_user(SID, "secret-ish builder text")
        self.cycle()
        r = self.c.call("GET", "/v1/journal?limit=50")
        self.assertNotIn("builder text", json.dumps(r))
        st = self.c.call("GET", "/v1/status")
        self.assertEqual([c["shown_through"] for c in st["consumers"] if c["name"] == "owner"], [0])

    def test_export_is_the_owners(self):
        out = os.path.join(self.h.tmp.name, "journal.jsonl")
        rc, body = run(self.h.home, "export", "--out", out)
        self.assertEqual(rc, 0, body)
        with open(out, encoding="utf-8") as f:
            lines = [json.loads(x) for x in f]
        self.assertEqual(len(lines), body["events"])
        d = self.claim()
        rc, body = run(self.h.home, "export", "--out", out, env=DIRECTOR_ENV)
        self.assertEqual(rc, cli.REFUSED)
        with self.assertRaises(client.ApiError) as e:  # and the API itself refuses the full journal
            d.call("GET", "/v1/journal?full=1")
        self.assertEqual(e.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
