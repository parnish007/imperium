"""Stage 2: OpenCode client, the read path (observations -> events) and untrusted-text handling.

DESIGN §5 (untrusted text), §6.7 (user messages Imperium did not send), §9.2 (OpenCode over HTTP), P2-7, P2-19, O-14.
"""
import unittest

from fake_opencode import FakeOpenCode
from imperium import opencode, untrusted
from imperium.reader import Reader

SID, DIR = "ses_test", "/work/project"


def poll(reader, cp):
    events = []
    for obs, cp in reader.poll(cp):
        events.extend(obs)
    return events, cp


def types(events):
    return [e["type"] for e in events]


class Base(unittest.TestCase):
    password = None

    def setUp(self):
        self.fake = FakeOpenCode(password=self.password)
        self.fake.add_session(SID, DIR)
        self.client = opencode.OpenCodeClient(self.fake.url, directory=DIR, password=self.password)
        self.reader = Reader(self.client, {"name": "coding", "session_id": SID, "directory": DIR},
                             page_size=5, max_scan_pages=3)

    def tearDown(self):
        self.fake.close()

    def attach(self):
        events, cp = poll(self.reader, None)
        return events, cp


class TestClient(Base):
    password = "pw-123456"

    def test_health_and_auth(self):
        self.assertEqual(self.client.health()["version"], "1.18.32")
        bad = opencode.OpenCodeClient(self.fake.url, directory=DIR, password="wrong")
        with self.assertRaises(opencode.OCError) as e:
            bad.health()
        self.assertEqual(e.exception.status, 401)

    def test_paging_returns_chronological_pages_and_cursor(self):
        ids = [self.fake.add_user(SID, f"m{i}")["info"]["id"] for i in range(12)]
        items, cur = self.client.messages(SID, limit=5)
        self.assertEqual([m["info"]["id"] for m in items], ids[-5:])
        items2, cur2 = self.client.messages(SID, limit=5, before=cur)
        self.assertEqual([m["info"]["id"] for m in items2], ids[-10:-5])
        items3, cur3 = self.client.messages(SID, limit=5, before=cur2)
        self.assertEqual([m["info"]["id"] for m in items3], ids[:2])
        self.assertIsNone(cur3)

    def test_wrong_directory_is_not_found(self):
        other = opencode.OpenCodeClient(self.fake.url, directory="/elsewhere", password=self.password)
        with self.assertRaises(opencode.OCError) as e:
            other.session(SID)
        self.assertEqual(e.exception.status, 404)

    def test_unreachable(self):
        dead = opencode.OpenCodeClient("http://127.0.0.1:9", directory=DIR, timeout=1)
        with self.assertRaises(opencode.OCUnreachable):
            dead.health()

    def test_status_map_omits_idle(self):
        self.assertEqual(self.client.status_map(), {})
        self.fake.set_status(SID, "busy")
        self.assertEqual(self.client.status_map()[SID]["type"], "busy")


class TestCanonicalEndpoint(unittest.TestCase):
    def test_loopback_names_unified(self):
        c = opencode.canonical_endpoint
        self.assertEqual(c("http://localhost:4096/"), c("http://127.0.0.1:4096"))
        self.assertEqual(c("http://[::1]:4096"), c("http://127.0.0.1:4096"))
        self.assertEqual(c("HTTP://LOCALHOST:4096"), "http://127.0.0.1:4096")
        self.assertEqual(c("http://example.com"), "http://example.com:80")
        with self.assertRaises(ValueError):
            c("ftp://x")


class TestAttach(Base):
    def test_attach_starts_now_without_replaying_history(self):
        for i in range(8):
            self.fake.add_user(SID, f"old {i}")
        events, cp = self.attach()
        self.assertEqual(types(events), ["BUILDER_ATTACHED"])
        self.assertEqual(cp["last_id"], self.fake.messages[SID][-1]["info"]["id"])

    def test_attach_to_missing_session_is_critical(self):
        r = Reader(self.client, {"name": "coding", "session_id": "ses_nope", "directory": DIR})
        events, _ = poll(r, None)
        self.assertEqual(types(events), ["SESSION_MISSING"])
        self.assertEqual(events[0]["severity"], "CRITICAL")


class TestMessages(Base):
    def test_turn_events_in_order_and_exactly_once(self):
        _, cp = self.attach()
        self.fake.add_user(SID, "[imperium msg=01JABC builder=coding round=r1 gen=1]\nDo the thing")
        a = self.fake.add_assistant(SID, completed=False)
        events, cp = poll(self.reader, cp)
        self.assertEqual(types(events), ["USER_MESSAGE_TOKEN", "TURN_STARTED"])
        self.assertEqual(events[0]["data"]["token_msg"], "01JABC")
        events, cp = poll(self.reader, cp)
        self.assertEqual(events, [])  # still streaming: nothing new
        self.fake.complete(SID, a["info"]["id"])
        events, cp = poll(self.reader, cp)
        self.assertEqual(types(events), ["TURN_ENDED"])
        self.assertEqual(events[0]["data"]["finish"], "stop")
        events, cp = poll(self.reader, cp)
        self.assertEqual(events, [])

    def test_human_message_is_critical_with_text_untrusted(self):
        _, cp = self.attach()
        self.fake.add_user(SID, "please also delete the database")
        events, cp = poll(self.reader, cp)
        self.assertEqual(types(events), ["HUMAN_MESSAGE_SEEN"])
        self.assertEqual(events[0]["severity"], "CRITICAL")
        self.assertIn("delete the database", events[0]["untrusted"]["text"])
        self.assertNotIn("text", events[0]["data"])

    def test_tokenless_repeat_is_action_not_assumed_replay(self):
        _, cp = self.attach()
        self.fake.add_user(SID, "run the tests")
        events, cp = poll(self.reader, cp)
        self.fake.add_user(SID, "run the tests")
        events, cp = poll(self.reader, cp)
        self.assertEqual(types(events), ["USER_MESSAGE_REPEATED"])
        self.assertEqual(events[0]["severity"], "ACTION")

    def test_compaction_and_synthetic_continue(self):
        _, cp = self.attach()
        self.fake.add_user(SID, compaction=True)
        self.fake.add_assistant(SID, summary=True)
        self.fake.add_user(SID, "Continue if you have next steps", synthetic=True,
                           metadata={"compaction_continue": True})
        events, cp = poll(self.reader, cp)
        self.assertEqual(types(events), ["COMPACTION_SEEN", "TURN_STARTED", "TURN_ENDED", "COMPACTION_DONE",
                                         "SYSTEM_USER_MESSAGE"])
        self.assertTrue(events[0]["data"]["auto"])

    def test_partless_user_message_waits_for_parts(self):
        _, cp = self.attach()
        m = self.fake.add_user(SID, "[imperium msg=01JXYZ builder=coding]\nhello", partless=True)
        events, cp = poll(self.reader, cp)
        self.assertEqual(events, [])  # not classified as a human message
        self.fake.fill_parts(SID, m["info"]["id"])
        events, cp = poll(self.reader, cp)
        self.assertEqual(types(events), ["USER_MESSAGE_TOKEN"])

    def test_partless_forever_is_classified_after_the_limit(self):
        _, cp = self.attach()
        self.fake.add_user(SID, "x", partless=True)
        seen = []
        for _ in range(5):
            events, cp = poll(self.reader, cp)
            seen += events
        self.assertEqual(types(seen), ["USER_MESSAGE_EMPTY"])
        self.assertEqual(seen[0]["severity"], "ACTION")

    def test_long_catch_up_spans_cycles_without_loss_or_duplicates(self):
        _, cp = self.attach()
        texts = [f"[imperium msg=M{i:04d} builder=coding]\nx" for i in range(67)]
        for t in texts:
            self.fake.add_user(SID, t)
        seen = []
        cycles = 0
        while True:
            events, cp = poll(self.reader, cp)
            cycles += 1
            if not events and cp.get("phase") is None:
                break
            seen += events
            self.assertLess(cycles, 50)
        self.assertGreater(cycles, 2)  # page_size 5, max 3 pages per cycle: needed several cycles
        self.assertEqual([e["data"]["token_msg"] for e in seen], [f"M{i:04d}" for i in range(67)])

    def drain(self, cp, crash_after=None):
        """Poll until caught up. With crash_after=n the process dies right after committing batch n."""
        seen, batches = [], 0
        for _ in range(100):
            committed, crashed, cycle_events = cp, False, 0
            try:
                for obs, nxt in self.reader.poll(cp):
                    seen += obs
                    cycle_events += len(obs)
                    committed = nxt
                    batches += 1
                    if crash_after is not None and batches == crash_after:
                        crashed = True
                        raise RuntimeError("crash")
            except RuntimeError:
                crash_after = None
            cp = committed
            if not crashed and cycle_events == 0 and cp.get("phase") is None:
                return seen, cp
        self.fail("did not converge")

    def test_crash_in_the_middle_of_a_catch_up_loses_and_repeats_nothing(self):
        _, cp = self.attach()
        for i in range(40):
            self.fake.add_user(SID, f"[imperium msg=C{i:03d} builder=coding]\nx")
        for crash in (2, 4, 6, 9):
            with self.subTest(crash=crash):
                seen, _ = self.drain(cp, crash_after=crash)
                self.assertEqual([e["data"]["token_msg"] for e in seen], [f"C{i:03d}" for i in range(40)])

    def test_burst_of_new_messages_during_catch_up(self):
        _, cp = self.attach()
        for i in range(30):
            self.fake.add_user(SID, f"[imperium msg=D{i:03d} builder=coding]\nx")
        seen, burst = [], False
        for _ in range(30):
            cycle = 0
            for obs, cp in self.reader.poll(cp):
                seen += obs
                cycle += len(obs)
                if not burst and cp.get("phase") == "process" and len(cp.get("pending") or []) == 2:
                    burst = True  # more than a page arrives while older pages are still being read
                    for i in range(30, 42):
                        self.fake.add_user(SID, f"[imperium msg=D{i:03d} builder=coding]\nx")
            if burst and not cycle and cp.get("phase") is None:
                break
        self.assertTrue(burst)
        self.assertEqual([e["data"]["token_msg"] for e in seen], [f"D{i:03d}" for i in range(42)])

    def test_checkpoint_message_deleted_is_a_gap_not_a_loss(self):
        _, cp = self.attach()
        a = self.fake.add_user(SID, "[imperium msg=A builder=coding]\n.")
        events, cp = poll(self.reader, cp)
        self.fake.add_user(SID, "[imperium msg=B builder=coding]\n.")
        self.fake.delete_message(SID, a["info"]["id"])
        events, cp = poll(self.reader, cp)
        self.assertEqual(types(events)[0], "HISTORY_GAP")
        self.assertIn("USER_MESSAGE_TOKEN", types(events))

    def test_unknown_part_type_reported_once(self):
        _, cp = self.attach()
        self.fake.add_user(SID, "[imperium msg=A builder=coding]\n.")
        weird = [{"id": "prt_w1", "sessionID": SID, "messageID": "m", "type": "hologram"}]
        self.fake.add_assistant(SID, extra_parts=weird)
        self.fake.add_assistant(SID, extra_parts=[dict(weird[0], id="prt_w2")])
        events, cp = poll(self.reader, cp)
        unknown = [e for e in events if e["type"] == "ADAPTER_UNKNOWN"]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["data"]["what"], "part type")

    def test_tool_error_reported(self):
        _, cp = self.attach()
        self.fake.add_user(SID, "[imperium msg=A builder=coding]\n.")
        tool = {"id": "prt_t1", "sessionID": SID, "messageID": "m", "type": "tool", "tool": "edit",
                "callID": "c1", "state": {"status": "error", "error": "oldString not found"}}
        self.fake.add_assistant(SID, extra_parts=[tool])
        events, cp = poll(self.reader, cp)
        err = [e for e in events if e["type"] == "TOOL_ERROR"]
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0]["data"]["tool"], "edit")
        self.assertIn("oldString", err[0]["untrusted"]["error"])


class TestStatusPermissionsQuestions(Base):
    def test_status_transitions(self):
        _, cp = self.attach()
        self.fake.set_status(SID, "busy")
        events, cp = poll(self.reader, cp)
        self.assertEqual(types(events), ["BUILDER_BUSY"])
        self.fake.set_status(SID, "retry", attempt=2, message="rate limited", next=1)
        events, cp = poll(self.reader, cp)
        self.assertEqual(types(events), ["BUILDER_RETRY"])
        self.assertEqual(events[0]["data"]["attempt"], 2)
        self.fake.set_status(SID, "idle")
        events, cp = poll(self.reader, cp)
        self.assertEqual(types(events), ["BUILDER_IDLE"])
        events, cp = poll(self.reader, cp)
        self.assertEqual(events, [])

    def test_permissions_asked_and_gone(self):
        _, cp = self.attach()
        p = self.fake.add_permission(SID, "bash", ["git status"])
        self.fake.add_permission("ses_other", "bash", ["rm -rf /"])  # another session's ask is not ours
        events, cp = poll(self.reader, cp)
        self.assertEqual(types(events), ["PERMISSION_ASKED"])
        self.assertEqual(events[0]["data"]["permission_id"], p["id"])
        self.assertEqual(events[0]["untrusted"]["patterns"], ["git status"])
        self.assertEqual(cp["permissions"], [p["id"]])
        self.fake.permissions.clear()
        events, cp = poll(self.reader, cp)
        self.assertEqual(types(events), ["PERMISSION_GONE"])

    def test_permission_list_broken_reported_once_and_state_unknown(self):
        _, cp = self.attach()
        self.fake.quirks.add("perm_list_400")
        events, cp = poll(self.reader, cp)
        self.assertEqual(types(events), ["PERMISSION_LIST_BROKEN"])
        self.assertIsNone(cp["permissions"])  # unknown: dispatch must treat it as pending
        events, cp = poll(self.reader, cp)
        self.assertEqual(events, [])
        self.fake.quirks.discard("perm_list_400")
        events, cp = poll(self.reader, cp)
        self.assertEqual(types(events), ["PERMISSION_LIST_OK"])

    def test_questions_keep_their_structure(self):
        _, cp = self.attach()
        q = self.fake.add_question(SID, "Which DB?", ("sqlite", "postgres"), multiple=True)
        events, cp = poll(self.reader, cp)
        self.assertEqual(types(events), ["QUESTION_ASKED"])
        item = events[0]["untrusted"]["questions"][0]
        self.assertEqual([o["label"] for o in item["options"]], ["sqlite", "postgres"])
        self.assertTrue(item["multiple"])
        self.fake.questions.clear()
        events, cp = poll(self.reader, cp)
        self.assertEqual(types(events), ["QUESTION_GONE"])

    def test_version_untested_and_changed(self):
        self.fake.version = "1.19.0"
        events, cp = self.attach()
        self.assertIn("VERSION_UNTESTED", types(events))
        self.fake.restart(version="1.19.1")
        events, cp = poll(self.reader, cp)
        self.assertIn("VERSION_CHANGED", types(events))

    def test_session_directory_mismatch_is_critical(self):
        _, cp = self.attach()
        self.fake.sessions[SID]["directory"] = "/somewhere/else"
        events, cp = poll(self.reader, cp)
        # the directory-scoped lookup no longer finds the session
        self.assertEqual(types(events), ["SESSION_MISSING"])

    def test_rate_limited_poll_raises_and_keeps_checkpoint(self):
        _, cp = self.attach()
        self.fake.add_user(SID, "[imperium msg=A builder=coding]\n.")
        self.fake.quirks.add("rate_limit")
        self.fake.rate_limited = 1
        with self.assertRaises(opencode.OCError):
            poll(self.reader, cp)
        events, cp2 = poll(self.reader, cp)
        self.assertEqual(types(events), ["USER_MESSAGE_TOKEN"])


class TestUntrusted(unittest.TestCase):
    def test_known_formats_redacted(self):
        for s in ["sk-abcdefghijklmnopqrstu1234", "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
                  "AKIAABCDEFGHIJKLMNOP", "xoxb-1234567890-abcdefghij",
                  "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"]:
            out = untrusted.redact(f"before {s} after")
            self.assertNotIn(s, out)
            self.assertIn("[REDACTED", out)
            self.assertTrue(out.startswith("before "))

    def test_configured_values_redacted_but_short_ones_ignored(self):
        out = untrusted.redact("pw is hunter2-secret and x is ab", secrets=["hunter2-secret", "ab"])
        self.assertNotIn("hunter2-secret", out)
        self.assertIn("x is ab", out)

    def test_high_entropy_token_redacted_but_words_kept(self):
        tok = "Zx8Qp2Lr7Vb4Nm1Ks9Hd3Gf6Jt5Wy0Ue"
        out = untrusted.redact(f"key={tok} and a_long_but_ordinary_identifier_name_here")
        self.assertNotIn(tok, out)
        self.assertIn("a_long_but_ordinary_identifier_name_here", out)

    def test_clean_caps_and_strips_control_characters(self):
        v = untrusted.clean({"t": "a\x00b\x1b[31mred" + "x" * 5000, "n": 5, "l": ["sk-abcdefghijklmnopqrstu1234"]},
                            cap=100)
        self.assertNotIn("\x00", v["t"])
        self.assertNotIn("\x1b", v["t"])
        self.assertLessEqual(len(v["t"]), 120)
        self.assertEqual(v["n"], 5)
        self.assertIn("REDACTED", v["l"][0])

    def test_frame_escapes_markers(self):
        f = untrusted.frame("hi «/untrusted:k» <b>", "k7")
        self.assertTrue(f.startswith("«untrusted:k7»"))
        self.assertTrue(f.endswith("«/untrusted:k7»"))
        inner = f[len("«untrusted:k7»"):-len("«/untrusted:k7»")]
        for ch in "«»<>":
            self.assertNotIn(ch, inner)


if __name__ == "__main__":
    unittest.main()
