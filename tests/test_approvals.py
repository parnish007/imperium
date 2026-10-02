"""Stage 5: approvals (rules, presence lease, held asks, the automatic-answer report), questions, path rules,
stop-all. Replies to OpenCode are operations, retried until delivered."""
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from fake_opencode import FakeOpenCode
from helpers import TempHome
from imperium import approvals, cli, client, opencode, tokens

SID, DIR = "ses_a1", "/work/approve"
CONFIG = "[opencode]\npoll_interval = 3600.0\n[delivery]\nidle_stable_polls = 1\n"


def run(home, *args, env=None):
    out, err = io.StringIO(), io.StringIO()
    rc = cli.main(["--home", home, "--json"] + list(args), out=out, err=err, env=env or {})
    return rc, (json.loads(out.getvalue()) if out.getvalue().strip() else None)


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

    def director(self):
        rc, body = run(self.h.home, "--as", "owner", "director", "claim", env={"CLAUDE_CODE_SESSION_ID": "s1"})
        self.assertEqual(rc, 0, body)
        return client.Client(self.h.home, env={"CLAUDE_CODE_SESSION_ID": "s1"})

    def ask(self, permission="bash", patterns=("git status",)):
        p = self.fake.add_permission(SID, permission=permission, patterns=patterns)
        self.cycle()
        return p["id"]

    def approval(self, aid):
        return self.c.call("GET", f"/v1/approval?id={aid}")["approval"]

    def rule(self, **kw):
        body = {"permission": "bash", "pattern": "git status*", "decision": "allow", **kw}
        return self.c.call("POST", "/v1/approvals/rules", body)


class TestByHand(Base):
    def test_owner_decides_reply_is_sent_and_dispatch_resumes(self):
        aid = self.ask()
        self.assertEqual(self.approval(aid)["state"], "PENDING")
        m = self.c.call("POST", "/v1/send", {"builder": "coding", "body": "go", "client_key": "k"})["message"]
        self.cycle()
        self.assertEqual(self.c.call("GET", f"/v1/message?id={m['id']}")["message"]["state"], "QUEUED")
        self.c.call("POST", "/v1/approvals/decide", {"id": aid, "reply": "once"})
        self.cycle()
        self.assertEqual(self.fake.permission_replies, [(aid, {"reply": "once"})])
        self.cycle(2)
        self.assertEqual(self.approval(aid)["reply_state"], "landed")
        self.assertEqual(self.c.call("GET", f"/v1/message?id={m['id']}")["message"]["state"], "ADMITTED")

    def test_first_decision_wins(self):
        aid = self.ask()
        self.c.call("POST", "/v1/approvals/decide", {"id": aid, "reply": "reject"})
        with self.assertRaises(client.ApiError) as e:
            self.c.call("POST", "/v1/approvals/decide", {"id": aid, "reply": "once"})
        self.assertEqual(e.exception.status, 409)

    def test_always_is_owner_only(self):
        d = self.director()
        aid = self.ask()
        with self.assertRaises(client.ApiError) as e:
            d.call("POST", "/v1/approvals/decide", {"id": aid, "reply": "always"})
        self.assertEqual(e.exception.status, 403)
        d.call("POST", "/v1/approvals/decide", {"id": aid, "reply": "once"})

    def test_reply_is_retried_until_delivered(self):
        aid = self.ask()
        self.c.call("POST", "/v1/approvals/decide", {"id": aid, "reply": "once"})
        with mock.patch.object(opencode.OpenCodeClient, "reply_permission",
                               side_effect=opencode.OCUnreachable("down")):
            self.cycle()
        self.assertEqual(self.approval(aid)["reply_state"], "pending")
        self.cycle()
        self.assertEqual(len(self.fake.permission_replies), 1)

    def test_reply_survives_a_restart(self):
        aid = self.ask()
        with mock.patch.object(opencode.OpenCodeClient, "reply_permission",
                               side_effect=opencode.OCUnreachable("down")):
            self.c.call("POST", "/v1/approvals/decide", {"id": aid, "reply": "once"})
            self.cycle()
        self.h.stop()
        self.h.start()
        self.c = self.h.client()
        self.cycle()
        self.assertEqual(len(self.fake.permission_replies), 1)

    def test_ask_that_disappears_expires(self):
        aid = self.ask()
        self.fake.permissions.clear()
        self.cycle()
        self.assertEqual(self.approval(aid)["state"], "EXPIRED")


class TestPolicy(Base):
    def test_rule_answers_only_with_a_present_director_and_is_reported(self):
        d = self.director()
        self.rule()
        d.call("POST", "/v1/events_since", {"consumer": "director"})  # the director is present
        aid = self.ask()
        a = self.approval(aid)
        self.assertEqual((a["state"], a["by_policy"]), ("APPROVED", True))
        self.cycle()
        self.assertEqual(self.fake.permission_replies, [(aid, {"reply": "once"})])
        self.assertEqual(self.c.call("GET", "/v1/status")["auto_answers_unreported"], 1)
        rc, body = run(self.h.home, "approvals", "--auto")
        self.assertEqual(rc, 0, body)
        self.assertEqual([x["id"] for x in body["approvals"]], [aid])
        self.assertEqual(self.c.call("GET", "/v1/status")["auto_answers_unreported"], 0)

    def test_without_a_lease_the_ask_is_held_and_a_later_lease_does_not_release_it(self):
        d = self.director()
        self.rule()
        aid = self.ask()  # the director never called anything: no lease
        self.assertEqual(self.approval(aid)["state"], "HELD")
        self.assertIn("PERMISSION_HELD", self.types())
        d.call("POST", "/v1/events_since", {"consumer": "director"})
        self.cycle(2)
        self.assertEqual(self.approval(aid)["state"], "HELD")
        d.call("POST", "/v1/approvals/decide", {"id": aid, "reply": "once"})
        self.assertEqual(self.approval(aid)["state"], "APPROVED")

    def test_lease_does_not_survive_a_restart(self):
        d = self.director()
        self.rule()
        d.call("POST", "/v1/events_since", {"consumer": "director"})
        self.h.stop()
        self.h.start()
        self.c = self.h.client()
        aid = self.ask()
        self.assertEqual(self.approval(aid)["state"], "HELD")

    def test_owner_calls_do_not_count_as_presence(self):
        self.director()
        self.rule()
        self.c.call("POST", "/v1/events_since", {"consumer": "owner"})
        aid = self.ask()
        self.assertEqual(self.approval(aid)["state"], "HELD")

    def test_an_unregistered_director_session_does_not_count_as_presence(self):
        self.director()
        with self.h.d.store.tx() as conn:
            raw = tokens.issue(conn, "director:other")
        tokens.write_locator(self.h.home, "director:other", raw)
        other = client.Client(self.h.home, env={"CLAUDE_CODE_SESSION_ID": "other"})
        self.rule()
        other.call("POST", "/v1/send", {"builder": "coding", "body": "x", "client_key": "o1"})
        aid = self.ask()
        self.assertEqual(self.approval(aid)["state"], "HELD")

    def test_deny_wins_and_unmatched_stays_pending(self):
        d = self.director()
        self.rule()
        self.rule(pattern="git status --porcelain*", decision="deny")
        d.call("POST", "/v1/events_since", {"consumer": "director"})
        aid = self.ask(patterns=("git status --porcelain",))
        self.assertEqual(self.approval(aid)["state"], "REJECTED")
        other = self.ask(patterns=("rm -rf build",))
        self.assertEqual(self.approval(other)["state"], "PENDING")

    def test_allow_must_cover_every_pattern(self):
        d = self.director()
        self.rule()
        d.call("POST", "/v1/events_since", {"consumer": "director"})
        aid = self.ask(patterns=("git status", "curl evil.example"))
        self.assertEqual(self.approval(aid)["state"], "PENDING")

    def test_rules_are_owner_only(self):
        d = self.director()
        with self.assertRaises(client.ApiError) as e:
            d.call("POST", "/v1/approvals/rules", {"permission": "bash", "pattern": "*", "decision": "allow"})
        self.assertEqual(e.exception.status, 403)

    def test_path_rule(self):
        d = self.director()
        root = tempfile.mkdtemp()
        self.rule(permission="edit", pattern="*", path_under=root)
        d.call("POST", "/v1/events_since", {"consumer": "director"})
        inside = self.ask(permission="edit", patterns=(os.path.join(root, "a.py"),))
        outside = self.ask(permission="edit", patterns=(os.path.join(root, "..", "evil.py"),))
        self.assertEqual(self.approval(inside)["state"], "APPROVED")
        self.assertEqual(self.approval(outside)["state"], "PENDING")


class TestQuestions(Base):
    def test_answer_with_structure(self):
        q = self.fake.add_question(SID, options=("A", "B"))
        self.cycle()
        qs = self.c.call("GET", "/v1/questions")["questions"]
        self.assertEqual(qs[0]["shape"][0]["options"], ["A", "B"])
        with self.assertRaises(client.ApiError) as e:
            self.c.call("POST", "/v1/questions/answer", {"id": q["id"], "answers": ["A"]})
        self.assertEqual(e.exception.status, 400)
        with self.assertRaises(client.ApiError):
            self.c.call("POST", "/v1/questions/answer", {"id": q["id"], "answers": [["A", "B"]]})  # single choice
        with self.assertRaises(client.ApiError) as e:  # two answers for one question
            self.c.call("POST", "/v1/questions/answer", {"id": q["id"], "answers": [["A"], ["B"]]})
        self.assertEqual(e.exception.status, 400)
        self.c.call("POST", "/v1/questions/answer", {"id": q["id"], "answers": [["B"]]})
        self.cycle()
        self.assertEqual(self.fake.question_replies, [(q["id"], {"kind": "reply", "answers": [["B"]]})])

    def test_reject_and_expire(self):
        q = self.fake.add_question(SID)
        q2 = self.fake.add_question(SID, text="other")
        self.cycle()
        self.c.call("POST", "/v1/questions/answer", {"id": q["id"], "reject": True})
        self.cycle()
        self.assertEqual(self.fake.question_replies[0][1]["kind"], "reject")
        self.fake.questions.clear()
        self.cycle()
        states = {x["id"]: x["state"] for x in self.c.call("GET", "/v1/questions?all=1")["questions"]}
        self.assertEqual(states[q2["id"]], "EXPIRED")


class TestStopAll(Base):
    def test_owner_stop_all_holds_asks_and_revokes_the_director(self):
        d = self.director()
        aid = self.ask()
        self.c.call("POST", "/v1/stop-all", {"reason": "leaving"})
        self.assertEqual(self.approval(aid)["state"], "HELD")
        with self.assertRaises(client.ApiError) as e:
            d.call("GET", "/v1/status")
        self.assertEqual(e.exception.status, 401)


class TestPathWithin(unittest.TestCase):
    def test_rules(self):
        root = tempfile.mkdtemp()
        self.assertTrue(approvals.path_within(os.path.join(root, "a", "b.py"), root))
        self.assertTrue(approvals.path_within("a/b.py", root))
        self.assertTrue(approvals.path_within(root, root))
        self.assertFalse(approvals.path_within(os.path.join(root, "..", "x"), root))
        self.assertFalse(approvals.path_within(root + "x", root))  # a sibling with the same prefix
        self.assertFalse(approvals.path_within("\\\\server\\share\\x", root))
        self.assertFalse(approvals.path_within("C:\\PROGRA~1\\x", root))
        self.assertFalse(approvals.path_within("~/x", root))
        self.assertTrue(approvals.path_within(os.path.join(root, "A.PY").upper(), root.upper(),
                                              case_insensitive=True))
        self.assertFalse(approvals.path_within("/R/a", "/r", case_insensitive=False))


if __name__ == "__main__":
    unittest.main()
