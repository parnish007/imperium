"""Stage 3: the outbox, dispatch eligibility, delivery proof, uncertainty and recovery (SYSTEM-DESIGN §2-§4).

The invariant checked everywhere: a message runs at most once on the builder unless a principal chose to
resend it, and every message ends in a recorded outcome.
"""
import io
import json
import os
import re
import unittest
from unittest import mock

from fake_opencode import FakeOpenCode
from helpers import TempHome
from imperium import cli, client, journal, opencode, outbox

SID, DIR = "ses_d1", "/work/deliver"
CONFIG = ("[opencode]\npoll_interval = 3600.0\n"
          "[delivery]\nidle_stable_polls = 1\nreconcile_window = 60.0\nadmit_timeout = 120.0\n")


def run(home, *args, env=None):
    out, err = io.StringIO(), io.StringIO()
    rc = cli.main(["--home", home, "--json"] + list(args), out=out, err=err, env=env or {})
    return rc, (json.loads(out.getvalue()) if out.getvalue().strip() else None)


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOpenCode()
        self.fake.add_session(SID, DIR)
        self.h = TempHome(CONFIG).init().start()
        self.now = 1_000_000.0
        self.h.d.engine.clock = lambda: self.now
        rc, body = run(self.h.home, "builder", "add", "coding", "--endpoint", self.fake.url, "--session", SID,
                       "--directory", DIR)
        self.assertEqual(rc, 0, body)
        self.c = self.h.client()
        self.cycle()  # attach

    def tearDown(self):
        self.h.cleanup()
        self.fake.close()

    def cycle(self, n=1):
        for _ in range(n):
            self.h.d.engine.run_once()

    def send(self, body="do the thing", key=None, **extra):
        payload = {"builder": "coding", "body": body, "client_key": key or os.urandom(6).hex(), **extra}
        return self.c.call("POST", "/v1/send", payload)["message"]

    def msg(self, mid):
        return self.c.call("GET", f"/v1/message?id={mid}")["message"]

    def types(self):
        with self.h.d.store.read() as conn:
            return [r[0] for r in conn.execute("SELECT type FROM events ORDER BY seq")]

    def until(self, mid, state, cycles=6):
        for _ in range(cycles):
            if self.msg(mid)["state"] == state:
                return
            self.cycle()
        self.assertEqual(self.msg(mid)["state"], state)

    def runs(self, mid):
        return self.fake.runs.get(self.msg(mid)["oc_message_id"], 0)


class TestHappyPath(Base):
    def test_delivered_then_admitted_and_ran_once(self):
        m = self.send()
        self.assertEqual(m["state"], "QUEUED")
        self.until(m["id"], "ADMITTED")
        got = self.msg(m["id"])
        self.assertRegex(got["oc_message_id"], r"^msg_[0-9a-f]{12}[0-9A-Za-z]{14}$")
        self.assertEqual(self.runs(m["id"]), 1)
        seq = [t for t in self.types() if t.startswith("MSG_")]
        self.assertEqual(seq, ["MSG_QUEUED", "MSG_DISPATCHING", "MSG_POSTED", "MSG_DELIVERED", "MSG_ADMITTED"])
        text = self.fake.find(SID, got["oc_message_id"])["parts"][0]["text"]
        self.assertTrue(text.startswith(f"[imperium msg={m['id']} builder=coding]"))

    def test_send_is_idempotent_by_client_key(self):
        a = self.send("same", key="k1")
        b = self.send("same", key="k1")
        self.assertEqual(a["id"], b["id"])
        with self.assertRaises(client.ApiError) as e:
            self.send("different", key="k1")
        self.assertEqual(e.exception.status, 409)

    def test_one_in_flight_per_builder(self):
        self.fake.auto_run = False
        a = self.send("first")
        b = self.send("second")
        self.cycle(3)
        self.assertEqual(self.msg(a["id"])["state"], "DELIVERED")
        self.assertEqual(self.msg(b["id"])["state"], "QUEUED")
        self.fake.run_pending(SID)
        self.until(a["id"], "ADMITTED")
        self.until(b["id"], "DELIVERED")

    def test_cancel_before_dispatch(self):
        self.fake.set_status(SID, "busy")
        self.cycle()
        m = self.send()
        self.c.call("POST", "/v1/cancel", {"id": m["id"]})
        self.fake.set_status(SID, "idle")
        self.cycle(3)
        self.assertEqual(self.msg(m["id"])["state"], "CANCELLED")
        self.assertEqual(self.runs(m["id"]), 0)


class TestEligibility(Base):
    def blocked_then_released(self, block, release):
        block()
        self.cycle()
        m = self.send()
        self.cycle(3)
        self.assertEqual(self.msg(m["id"])["state"], "QUEUED")
        release()
        self.until(m["id"], "ADMITTED")

    def test_busy(self):
        self.blocked_then_released(lambda: self.fake.set_status(SID, "busy"),
                                   lambda: self.fake.set_status(SID, "idle"))

    def test_permission_pending(self):
        self.blocked_then_released(lambda: self.fake.add_permission(SID), lambda: self.fake.permissions.clear())

    def test_question_pending(self):
        self.blocked_then_released(lambda: self.fake.add_question(SID), lambda: self.fake.questions.clear())

    def test_permission_list_broken_counts_as_pending(self):
        self.blocked_then_released(lambda: self.fake.quirks.add("perm_list_400"),
                                   lambda: self.fake.quirks.discard("perm_list_400"))

    def test_last_reply_incomplete(self):
        def block():
            self.fake.add_user(SID, "[imperium msg=X builder=coding]\n.")
            self.a = self.fake.add_assistant(SID, completed=False)
        self.blocked_then_released(block, lambda: self.fake.complete(SID, self.a["info"]["id"]))

    def test_busy_sub_agent(self):
        self.fake.add_session("ses_child", DIR, parent=SID)
        self.blocked_then_released(lambda: self.fake.set_status("ses_child", "busy"),
                                   lambda: self.fake.set_status("ses_child", "idle"))

    def test_untested_version_needs_the_owner(self):
        def block():
            self.fake.version = "9.9.9"
        def release():
            rc, body = run(self.h.home, "builder", "allow-version", "coding", "9.9.9")
            self.assertEqual(rc, 0, body)
        self.blocked_then_released(block, release)

    def test_pause_stops_director_not_owner(self):
        from imperium import tokens
        with self.h.d.store.tx() as conn:
            raw = tokens.issue(conn, "director:s1")
        tokens.write_locator(self.h.home, "director:s1", raw)
        director = client.Client(self.h.home, env={"CLAUDE_CODE_SESSION_ID": "s1"})
        self.c.call("POST", "/v1/builders/pause", {"name": "coding"})
        d = director.call("POST", "/v1/send", {"builder": "coding", "body": "from director", "client_key": "d1"})
        o = self.send("from owner")
        self.cycle(4)
        self.assertEqual(self.msg(o["id"])["state"], "ADMITTED")
        self.assertEqual(self.msg(d["message"]["id"])["state"], "QUEUED")
        self.c.call("POST", "/v1/builders/resume", {"name": "coding"})
        self.until(d["message"]["id"], "ADMITTED")

    def test_stalled_queue_raises_one_action_event(self):
        self.fake.set_status(SID, "busy")
        self.cycle()
        self.send()
        self.cycle()
        self.assertNotIn("DISPATCH_STALLED", self.types())
        self.now += 601
        self.cycle(3)
        self.assertEqual(self.types().count("DISPATCH_STALLED"), 1)

    def test_stop_all_stops_everyone_until_resume(self):
        self.c.call("POST", "/v1/stop-all", {})
        m = self.send()
        self.cycle(3)
        self.assertEqual(self.msg(m["id"])["state"], "QUEUED")
        self.c.call("POST", "/v1/resume-all", {})
        self.until(m["id"], "ADMITTED")


class TestUncertainty(Base):
    def test_error_after_persist_is_found_by_id_not_resent(self):
        self.fake.quirks.add("error_after_persist")
        m = self.send()
        self.cycle()
        self.assertEqual(self.msg(m["id"])["state"], "UNKNOWN")
        self.fake.quirks.clear()
        self.until(m["id"], "ADMITTED")
        self.assertEqual(self.runs(m["id"]), 1)

    def test_dropped_connection_after_persist(self):
        self.fake.quirks.add("drop_after_persist")
        m = self.send()
        self.cycle()
        self.assertEqual(self.msg(m["id"])["state"], "UNKNOWN")
        self.fake.quirks.clear()
        self.until(m["id"], "ADMITTED")
        self.assertEqual(self.runs(m["id"]), 1)

    def test_error_before_persist_becomes_uncertain_and_is_never_resent_automatically(self):
        self.fake.quirks.add("error_before_persist")
        m = self.send()
        self.cycle()
        self.fake.quirks.clear()
        self.cycle(3)
        self.assertEqual(self.msg(m["id"])["state"], "UNKNOWN")
        self.now += 61
        self.cycle()
        self.assertEqual(self.msg(m["id"])["state"], "UNCERTAIN")
        self.assertIn("MSG_UNCERTAIN", self.types())
        self.now += 3600
        self.cycle(3)
        self.assertEqual(self.msg(m["id"])["state"], "UNCERTAIN")  # no automatic resend, ever
        self.assertEqual(self.runs(m["id"]), 0)

    def test_answered_204_but_never_saved(self):
        self.fake.quirks.add("fail_async")
        m = self.send()
        self.cycle()
        self.assertEqual(self.msg(m["id"])["state"], "POSTED")
        self.fake.quirks.clear()
        self.now += 61
        self.cycle()
        self.assertEqual(self.msg(m["id"])["state"], "UNCERTAIN")

    def test_resend_needs_confirmation_and_links_the_original(self):
        self.fake.quirks.add("error_before_persist")
        m = self.send()
        self.cycle()
        self.fake.quirks.clear()
        self.now += 61
        self.cycle()
        with self.assertRaises(client.ApiError):
            self.c.call("POST", "/v1/message/resolve", {"id": m["id"], "choice": "resend"})
        r = self.c.call("POST", "/v1/message/resolve", {"id": m["id"], "choice": "resend",
                                                        "confirm_may_run_twice": True})
        self.assertEqual(self.msg(m["id"])["state"], "SUPERSEDED")
        new = r["resent_as"]
        self.assertEqual(self.msg(new)["supersedes"], m["id"])
        self.until(new, "ADMITTED")
        self.assertEqual(self.runs(new), 1)

    def test_uncertain_found_later_moves_to_delivered(self):
        self.fake.quirks.add("fail_async")
        m = self.send()
        self.cycle()
        self.now += 61
        self.cycle()
        self.assertEqual(self.msg(m["id"])["state"], "UNCERTAIN")
        self.fake.quirks.clear()
        self.fake.add_user(SID, f"[imperium msg={m['id']} builder=coding]\nlate", id=self.msg(m["id"])["oc_message_id"])
        self.until(m["id"], "DELIVERED")
        self.assertIn("MSG_FOUND_LATE", self.types())


class TestStranded(Base):
    def test_saved_but_never_run_is_stranded_and_blocks_the_builder(self):
        self.fake.quirks.add("drop_prompt")
        m = self.send("first")
        other = self.send("second")
        self.until(m["id"], "DELIVERED")
        self.now += 121
        self.cycle()
        self.assertEqual(self.msg(m["id"])["state"], "STRANDED")
        self.cycle(2)
        self.assertEqual(self.msg(other["id"])["state"], "QUEUED")  # nothing else until resolved
        self.fake.quirks.clear()
        self.c.call("POST", "/v1/message/resolve", {"id": m["id"], "choice": "cancel"})
        self.until(other["id"], "ADMITTED")

    def test_cancelled_message_that_runs_later_is_reported(self):
        self.fake.quirks.add("drop_prompt")
        m = self.send()
        self.until(m["id"], "DELIVERED")
        self.now += 121
        self.cycle()
        self.c.call("POST", "/v1/message/resolve", {"id": m["id"], "choice": "cancel"})
        self.fake.run_pending(SID)
        self.cycle(2)
        self.assertEqual(self.msg(m["id"])["state"], "CANCELLED")
        self.assertIn("LATE_ADMISSION", self.types())

    def test_resent_and_original_both_ran_is_critical(self):
        self.fake.quirks.add("drop_prompt")
        m = self.send()
        self.until(m["id"], "DELIVERED")
        self.now += 121
        self.cycle()
        self.fake.quirks.clear()
        r = self.c.call("POST", "/v1/message/resolve", {"id": m["id"], "choice": "resend",
                                                        "confirm_may_run_twice": True})
        self.until(r["resent_as"], "ADMITTED")
        self.fake.run_pending(SID)  # the stranded original finally runs too
        self.cycle(2)
        self.assertIn("DUPLICATE_RAN", self.types())


class TestReplayAndRejection(Base):
    def test_replay_copy_under_foreign_id_is_not_proof(self):
        self.fake.quirks.add("fail_async")
        m = self.send()
        self.cycle()
        self.fake.add_user(SID, f"[imperium msg={m['id']} builder=coding]\ncopy")  # e.g. a compaction replay
        self.cycle(2)
        self.assertEqual(self.msg(m["id"])["state"], "POSTED")
        self.assertIn("REPLAY_SEEN", self.types())

    def test_definite_rejection_is_not_sent(self):
        m = self.send()
        with mock.patch.object(opencode.OpenCodeClient, "prompt_async",
                               side_effect=opencode.OCError(400, {"error": "bad parts"}, "/prompt_async")):
            self.cycle()
        self.assertEqual(self.msg(m["id"])["state"], "REJECTED")
        n = self.send("next")
        self.until(n["id"], "ADMITTED")  # the reservation was released


class TestRecovery(Base):
    def test_crash_while_dispatching_is_reconciled_not_resent(self):
        m = self.send()
        real = opencode.OpenCodeClient.prompt_async

        def post_then_die(self_, *a, **kw):
            real(self_, *a, **kw)
            raise SystemExit("daemon killed after the POST, before recording it")

        with mock.patch.object(opencode.OpenCodeClient, "prompt_async", post_then_die):
            with self.assertRaises(SystemExit):
                self.h.d.engine.run_once()
        self.assertEqual(self.msg(m["id"])["state"], "DISPATCHING")
        self.h.stop()
        self.h.start()
        self.h.d.engine.clock = lambda: self.now
        self.c = self.h.client()
        self.assertEqual(self.msg(m["id"])["state"], "UNKNOWN")
        self.until(m["id"], "ADMITTED")
        self.assertEqual(self.runs(m["id"]), 1)

    def test_crash_before_the_post_never_sent(self):
        m = self.send()
        with mock.patch.object(opencode.OpenCodeClient, "prompt_async", side_effect=SystemExit("killed")):
            with self.assertRaises(SystemExit):
                self.h.d.engine.run_once()
        self.h.stop()
        self.h.start()
        self.h.d.engine.clock = lambda: self.now
        self.c = self.h.client()
        self.now += 61
        self.cycle(2)
        self.assertEqual(self.msg(m["id"])["state"], "UNCERTAIN")  # absence is not proof: a decision is needed
        self.assertEqual(self.runs(m["id"]), 0)


class TestCli(Base):
    def test_send_queue_show_resolve(self):
        rc, body = run(self.h.home, "send", "coding", "--message", "hello", "--key", "cli-1")
        self.assertEqual(rc, 0, body)
        mid = body["message"]["id"]
        rc, body = run(self.h.home, "queue", "coding")
        self.assertIn(mid, [m["id"] for m in body["messages"]])
        self.cycle(4)
        rc, body = run(self.h.home, "msg", "show", mid)
        self.assertEqual(body["message"]["state"], "ADMITTED")


if __name__ == "__main__":
    unittest.main()
