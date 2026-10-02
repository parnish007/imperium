"""Daemon and local API: Host/Origin rules, bearer tokens, rate limits, fail closed, recovery (DESIGN §3, §11)."""
import http.client
import json
import os
import time
import unittest

from imperium import client, daemon, journal, tokens
from imperium.store import Store

from helpers import TempHome


class Base(unittest.TestCase):
    config = None

    def setUp(self):
        self.h = TempHome(self.config).init().start()
        self.c = self.h.client()

    def tearDown(self):
        self.h.cleanup()

    def raw(self, method, path, body=None, headers=None):
        port = self.h.d.port
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        hdrs = {"Host": f"127.0.0.1:{port}"}
        hdrs.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=hdrs)
        r = conn.getresponse()
        out = r.status, json.loads(r.read() or b"{}")
        conn.close()
        return out

    def append(self, severity, type_="X"):
        with self.h.d.store.tx() as conn:
            return journal.append(conn, type_, severity)


class TestTransport(Base):
    def test_health_needs_no_token(self):
        status, body = self.raw("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["run_id"], self.h.d.run_id)

    def test_status_needs_a_token(self):
        self.assertEqual(self.raw("GET", "/v1/status")[0], 401)
        self.assertEqual(self.raw("GET", "/v1/status", headers={"Authorization": "Bearer nope"})[0], 401)
        self.assertTrue(self.c.call("GET", "/v1/status")["ok"])

    def test_foreign_host_refused(self):
        status, _ = self.raw("GET", "/v1/health", headers={"Host": "evil.example:80"})
        self.assertEqual(status, 403)

    def test_foreign_origin_refused(self):
        status, _ = self.raw("GET", "/v1/health", headers={"Origin": "http://127.0.0.1:9"})
        self.assertEqual(status, 403)

    def test_unknown_route(self):
        with self.assertRaises(client.ApiError) as e:
            self.c.call("GET", "/v1/nope")
        self.assertEqual(e.exception.status, 404)

    def test_body_too_large(self):
        with self.assertRaises(client.ApiError) as e:
            self.c.call("POST", "/v1/ack", {"consumer": "owner", "seq": 0, "pad": "x" * 300_000})
        self.assertEqual(e.exception.status, 413)

    def test_reads_are_audited_not_journaled(self):
        with self.h.d.store.read() as conn:
            before = journal.head(conn)[0]
        for _ in range(5):
            self.c.call("GET", "/v1/status")
        # the audit row is written after the response is sent (it records the result): give the last one a moment
        deadline = time.monotonic() + 5
        while True:
            with self.h.d.store.read() as conn:
                n = conn.execute("SELECT COUNT(*) FROM call_audit WHERE route='GET /v1/status'").fetchone()[0]
            if n >= 5 or time.monotonic() > deadline:
                break
            time.sleep(0.05)
        self.assertEqual(n, 5)
        with self.h.d.store.read() as conn:
            self.assertEqual(journal.head(conn)[0], before)


class TestFeedsApi(Base):
    def test_page_ack_show(self):
        a = self.append("ACTION")
        r = self.c.call("POST", "/v1/events_since", {"consumer": "owner", "limit": 10})
        self.assertIn(a, [e["seq"] for e in r["events"]])
        ev = self.c.call("GET", f"/v1/show?seq={a}")["event"]
        self.assertEqual(ev["seq"], a)
        self.c.call("POST", "/v1/ack", {"consumer": "owner", "seq": r["high_water"]})

    def test_ack_beyond_shown_is_409(self):
        a = self.append("ACTION")
        with self.assertRaises(client.ApiError) as e:
            self.c.call("POST", "/v1/ack", {"consumer": "owner", "seq": a})
        self.assertEqual(e.exception.status, 409)

    def test_get_never_changes_state(self):
        a = self.append("ACTION")
        self.c.call("GET", f"/v1/show?seq={a}")
        with self.assertRaises(client.ApiError):
            self.c.call("POST", "/v1/ack", {"consumer": "owner", "seq": a})


class TestAuthority(Base):
    def director(self):
        with self.h.d.store.tx() as conn:
            raw = tokens.issue(conn, "director:s1")
        tokens.write_locator(self.h.home, "director:s1", raw)
        return client.Client(self.h.home, env={"CLAUDE_CODE_SESSION_ID": "s1"})

    def test_director_cannot_prune_or_back_up(self):
        d = self.director()
        for path, body in [("/v1/prune", {"through": 1}), ("/v1/backup", {}), ("/v1/shutdown", {})]:
            with self.assertRaises(client.ApiError) as e:
                d.call("POST", path, body)
            self.assertEqual(e.exception.status, 403, path)

    def test_director_cannot_read_owner_feed(self):
        d = self.director()
        with self.assertRaises(client.ApiError) as e:
            d.call("POST", "/v1/events_since", {"consumer": "owner"})
        self.assertEqual(e.exception.status, 403)

    def test_director_creates_its_own_feed(self):
        d = self.director()
        d.call("POST", "/v1/consumers", {"name": "director", "floor": "ACTION"})
        self.append("ACTION")
        r = d.call("POST", "/v1/events_since", {"consumer": "director"})
        self.assertEqual(len(r["events"]), 1)

    def test_token_rotation_revokes_the_old_token(self):
        old = tokens.read_locator(self.h.home, "owner")
        self.c.call("POST", "/v1/token/rotate", {})
        new = tokens.read_locator(self.h.home, "owner")
        self.assertNotEqual(old, new)
        status, _ = self.raw("GET", "/v1/status", headers={"Authorization": f"Bearer {old}"})
        self.assertEqual(status, 401)
        self.assertTrue(self.h.client().call("GET", "/v1/status")["ok"])


class TestRateLimit(Base):
    config = "[daemon]\nrate_per_sec = 1.0\nburst = 3\n"

    def test_burst_then_429(self):
        codes = []
        for _ in range(6):
            try:
                self.c.call("GET", "/v1/status")
                codes.append(200)
            except client.ApiError as e:
                codes.append(e.status)
        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertIn(429, codes[3:])


class TestFailClosed(Base):
    def test_write_failure_is_reported_and_refuses_writes(self):
        self.h.d.store.failed = "journal write failed: disk I/O error"
        with self.assertRaises(client.ApiError) as e:
            self.c.call("POST", "/v1/events_since", {"consumer": "owner"})
        self.assertEqual(e.exception.status, 503)
        st = self.c.call("GET", "/v1/status")
        self.assertIn("disk I/O error", st["critical"])


class TestLifecycle(unittest.TestCase):
    def test_second_daemon_refused(self):
        h = TempHome().init().start()
        try:
            with self.assertRaises(daemon.AlreadyRunning):
                daemon.Daemon(h.home).start()
        finally:
            h.cleanup()

    def test_start_records_events_and_detects_a_broken_chain(self):
        h = TempHome().init()
        try:
            h.start()
            h.stop()
            s = Store(os.path.join(h.home, "imperium.db"))
            with s.tx() as conn:
                conn.execute("UPDATE events SET data='{\"x\":1}' WHERE seq=1")
            s.close()
            h.start()
            st = h.client().call("GET", "/v1/status")
            self.assertFalse(st["chain"]["ok"])
            with h.d.store.read() as conn:
                types = [r[0] for r in conn.execute("SELECT type FROM events ORDER BY seq")]
            self.assertIn("INTEGRITY_FAIL", types)
            self.assertEqual(types.count("DAEMON_STARTED"), 2)
        finally:
            h.cleanup()

    def test_shutdown_by_owner(self):
        h = TempHome().init().start()
        try:
            h.client().call("POST", "/v1/shutdown", {})
            self.assertTrue(h.d.stopped.wait(5))
            h.thread.join(5)
            self.assertFalse(h.thread.is_alive())
            h.d = None
        finally:
            h.cleanup()


if __name__ == "__main__":
    unittest.main()
