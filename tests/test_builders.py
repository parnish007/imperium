"""Stage 2: builder registry and the daemon's polling engine (DESIGN §4 Builder, §9.2, §10.3, P2-8)."""
import io
import json
import os
import unittest

from fake_opencode import FakeOpenCode
from helpers import TempHome
from imperium import cli, client, journal

SID, DIR = "ses_b1", "/work/one"
QUIET = "[opencode]\npoll_interval = 3600.0\n"  # tests drive the engine by hand


def run(home, *args, env=None):
    out, err = io.StringIO(), io.StringIO()
    rc = cli.main(["--home", home, "--json"] + list(args), out=out, err=err, env=env or {})
    return rc, (json.loads(out.getvalue()) if out.getvalue().strip() else None)


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeOpenCode(password="pw-abcdef")
        self.fake.add_session(SID, DIR)
        self.h = TempHome(QUIET).init()
        os.environ["IMPERIUM_TEST_OC_PW"] = "pw-abcdef"
        self.h.start()

    def tearDown(self):
        self.h.cleanup()
        self.fake.close()
        os.environ.pop("IMPERIUM_TEST_OC_PW", None)

    def add(self, name="coding", endpoint=None, session=SID, directory=DIR, extra=()):
        return run(self.h.home, "builder", "add", name, "--endpoint", endpoint or self.fake.url,
                   "--session", session, "--directory", directory, "--password-env", "IMPERIUM_TEST_OC_PW", *extra)

    def events(self, type_=None):
        with self.h.d.store.read() as conn:
            rows = conn.execute("SELECT seq FROM events ORDER BY seq").fetchall()
            evs = [journal.get(conn, r[0]) for r in rows]
        return [e for e in evs if type_ is None or e["type"] == type_]

    def cycle(self):
        self.h.d.engine.run_once()


class TestRegistry(Base):
    def test_add_list_show_remove(self):
        rc, body = self.add()
        self.assertEqual(rc, 0, body)
        self.assertEqual(body["builder"]["opencode_version"], "1.18.32")
        rc, body = run(self.h.home, "builder", "list")
        self.assertEqual([b["name"] for b in body["builders"]], ["coding"])
        self.assertNotIn("pw-abcdef", json.dumps(body))  # only the variable's name is stored
        rc, body = run(self.h.home, "builder", "show", "coding")
        self.assertEqual(body["builder"]["password_env"], "IMPERIUM_TEST_OC_PW")
        rc, body = run(self.h.home, "builder", "remove", "coding")
        self.assertEqual(rc, 0)
        rc, body = run(self.h.home, "builder", "list")
        self.assertEqual(body["builders"], [])

    def test_same_session_through_another_loopback_name_refused(self):
        self.add()
        rc, body = self.add("coding2", endpoint=self.fake.url.replace("127.0.0.1", "localhost"))
        self.assertEqual(rc, 4, body)
        self.assertIn("already registered", body["error"])

    def test_overlapping_workspace_refused(self):
        self.fake.add_session("ses_b2", DIR + "/sub")
        self.add()
        rc, body = self.add("inner", session="ses_b2", directory=DIR + "/sub")
        self.assertEqual(rc, 4, body)
        self.assertIn("overlaps", body["error"])

    def test_sibling_with_common_prefix_is_not_overlap(self):
        self.fake.add_session("ses_b3", DIR + "x")
        self.add()
        rc, body = self.add("sibling", session="ses_b3", directory=DIR + "x")
        self.assertEqual(rc, 0, body)

    def test_bad_name_refused(self):
        rc, body = self.add("Bad Name")
        self.assertEqual(rc, 1)

    def test_unknown_session_refused(self):
        rc, body = self.add(session="ses_missing")
        self.assertEqual(rc, 4)
        self.assertIn("not found", body["error"])

    def test_director_cannot_add_builders(self):
        from imperium import tokens
        with self.h.d.store.tx() as conn:
            raw = tokens.issue(conn, "director:s1")
        tokens.write_locator(self.h.home, "director:s1", raw)
        rc, body = run(self.h.home, "builder", "add", "x", "--endpoint", self.fake.url, "--session", SID,
                       "--directory", DIR, env={"CLAUDE_CODE_SESSION_ID": "s1"})
        self.assertEqual(rc, 4)


class TestEngine(Base):
    def test_events_flow_and_are_exactly_once_across_restart(self):
        self.add()
        self.cycle()
        self.assertEqual(len(self.events("BUILDER_ATTACHED")), 1)
        self.fake.add_user(SID, "[imperium msg=01JA builder=coding]\nwork")
        self.fake.add_assistant(SID)
        self.cycle()
        self.cycle()
        self.assertEqual(len(self.events("USER_MESSAGE_TOKEN")), 1)
        self.assertEqual(len(self.events("TURN_ENDED")), 1)
        self.h.stop()
        self.h.start()
        self.h.d.engine.run_once()
        self.assertEqual(len(self.events("USER_MESSAGE_TOKEN")), 1)
        self.assertEqual(len(self.events("BUILDER_ATTACHED")), 1)
        with self.h.d.store.read() as conn:
            self.assertTrue(journal.verify_chain(conn).ok)

    def test_events_carry_builder_name_and_redacted_text(self):
        self.add()
        self.cycle()
        self.fake.add_user(SID, "my key is sk-abcdefghijklmnopqrstu1234 and pw-abcdef")
        self.cycle()
        ev = self.events("HUMAN_MESSAGE_SEEN")[0]
        self.assertEqual(ev["builder"], "coding")
        text = ev["untrusted"]["text"]
        self.assertNotIn("sk-abcdefghijklmnopqrstu1234", text)
        self.assertNotIn("pw-abcdef", text)  # the builder's own password is a configured secret

    def test_unreachable_reported_once_then_recovered(self):
        self.add()
        self.cycle()
        port = self.fake.port
        self.fake.close()
        for _ in range(5):
            self.cycle()
        self.assertEqual(len(self.events("BUILDER_UNREACHABLE")), 1)
        self.fake = FakeOpenCode(password="pw-abcdef")
        self.fake.add_session(SID, DIR)
        # point the builder at the new port (a restart on another port is a re-registration in v1)
        with self.h.d.store.tx() as conn:
            conn.execute("UPDATE builders SET endpoint=? WHERE name='coding'", (self.fake.url,))
        self.h.d.engine.reset_backoff()
        self.cycle()
        self.assertEqual(len(self.events("BUILDER_REACHABLE")), 1)

    def test_wrong_password_is_reported(self):
        os.environ["IMPERIUM_TEST_OC_PW"] = "wrong"
        rc, body = self.add(extra=("--no-check",))
        self.assertEqual(rc, 0, body)
        self.cycle()
        self.assertEqual(len(self.events("BUILDER_AUTH_FAILED")), 1)

    def test_status_shows_builders(self):
        self.add()
        self.cycle()
        st = self.h.client().call("GET", "/v1/status")
        self.assertEqual(st["builders"][0]["name"], "coding")
        self.assertTrue(st["builders"][0]["reachable"])


if __name__ == "__main__":
    unittest.main()
