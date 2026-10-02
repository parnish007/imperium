"""Regression tests for the external critique of stage 1-2 (2026-10-02). Each test reproduces one confirmed defect.

C1 forged high-water mark · C4 token rotation crash windows · C5 backup onto live files · C6 broken chain must
quarantine · C7 restore must verify the journal · C8 source keys bound to content and kept after pruning ·
C9 director provisioning exists · C14 unexpected IntegrityError fails closed · C16 status says when the chain
was checked · C17 atomic file writes.
"""
import io
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from helpers import TempHome
from imperium import backup, cli, client, feeds, fsutil, journal, retention, tokens
from imperium.store import Store, StoreFailed


def run(home, *args, env=None):
    out, err = io.StringIO(), io.StringIO()
    rc = cli.main(["--home", home, "--json"] + list(args), out=out, err=err, env=env or {})
    return rc, (json.loads(out.getvalue()) if out.getvalue().strip() else None)


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.store = Store(os.path.join(self.home, "imperium.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def append(self, sev="ACTION", **kw):
        with self.store.tx() as c:
            return journal.append(c, "X", sev, **kw)


class TestC1ForgedHighWater(StoreBase):
    def test_client_high_water_is_capped_at_the_head(self):
        with self.store.tx() as c:
            feeds.create(c, "d", principal="p", floor="ACTION")
        self.append()
        with self.store.tx() as c:
            r = feeds.events_since(c, "d", "p", limit=10, high_water=999_999_999)
        self.assertEqual(r["high_water"], 1)
        later = self.append()
        with self.store.tx() as c:
            with self.assertRaises(feeds.Refused):
                feeds.ack(c, "d", "p", later)
            self.assertLessEqual(feeds.get(c, "d")["shown_through"], 1)


class TestC8SourceKeys(StoreBase):
    def test_same_key_different_content_is_a_violation(self):
        self.append(source_key="k1", data={"a": 1})
        with self.assertRaises(journal.SourceKeyConflict):
            self.append(source_key="k1", data={"a": 2})

    def test_key_survives_pruning(self):
        self.append("INFO", source_key="k1", data={"a": 1})
        self.append("INFO")
        retention.prune(self.store, os.path.join(self.home, "archive"), 1)
        seq = self.append("INFO", source_key="k1", data={"a": 1})
        with self.store.read() as c:
            self.assertIsNone(journal.get(c, seq))  # the original, now archived: nothing new was inserted
            self.assertEqual(c.execute("SELECT COUNT(*) FROM events WHERE source_key='k1'").fetchone()[0], 0)


class TestC14IntegrityErrors(StoreBase):
    def test_unexpected_integrity_error_fails_closed(self):
        with self.assertRaises(StoreFailed):
            with self.store.tx() as c:
                c.execute("INSERT INTO meta(key, value) VALUES('schema_version', 'dup')")
        self.assertIsNotNone(self.store.failed)


class TestC17AtomicWrite(unittest.TestCase):
    def test_failed_replace_keeps_the_old_content(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "f")
            fsutil.atomic_write(p, "old")
            with mock.patch.object(fsutil.os, "replace", side_effect=OSError("power cut")):
                with self.assertRaises(OSError):
                    fsutil.atomic_write(p, "new")
            with open(p, encoding="utf-8") as f:
                self.assertEqual(f.read(), "old")
            self.assertEqual(sorted(os.listdir(d)), ["f"])  # no temp file left behind


class DaemonBase(unittest.TestCase):
    def setUp(self):
        self.h = TempHome().init().start()
        self.c = self.h.client()

    def tearDown(self):
        self.h.cleanup()


class TestC4TokenRotation(DaemonBase):
    def test_crash_before_locator_write_keeps_old_token_working(self):
        old = tokens.read_locator(self.h.home, "owner")
        with mock.patch.object(tokens, "write_locator", side_effect=OSError("disk full")):
            with self.assertRaises(client.ApiError):
                self.c.call("POST", "/v1/token/rotate", {})
        self.assertEqual(tokens.read_locator(self.h.home, "owner"), old)
        self.assertTrue(self.h.client().call("GET", "/v1/status")["ok"])

    def test_crash_after_locator_write_is_finished_at_next_start(self):
        old = tokens.read_locator(self.h.home, "owner")
        with mock.patch.object(tokens, "revoke_except", side_effect=RuntimeError("crash")):
            with self.assertRaises(client.ApiError):
                self.c.call("POST", "/v1/token/rotate", {})
        new = tokens.read_locator(self.h.home, "owner")
        self.assertNotEqual(new, old)
        self.assertTrue(self.h.client().call("GET", "/v1/status")["ok"])  # the locator's token works
        self.h.stop()
        self.h.start()  # start-up finishes the rotation: only the locator's token stays valid
        with self.h.d.store.read() as conn:
            self.assertIsNone(tokens.verify(conn, old))
            self.assertEqual(tokens.verify(conn, new), "owner")


class TestC5BackupDestination(DaemonBase):
    def test_backup_onto_live_files_refused(self):
        for name in ("imperium.db", "imperium.db-wal", "imperium.db-shm", "daemon.json", "imperium.toml",
                     os.path.join("tokens", "owner"), "anything-inside-home.db"):
            with self.subTest(name=name):
                with self.assertRaises(client.ApiError) as e:
                    self.c.call("POST", "/v1/backup", {"dest": os.path.join(self.h.home, name)})
                self.assertEqual(e.exception.status, 400)
        with self.h.d.store.read() as conn:
            self.assertTrue(journal.verify_chain(conn).ok)

    def test_existing_file_not_overwritten(self):
        dest = os.path.join(self.h.tmp.name, "b.db")
        self.c.call("POST", "/v1/backup", {"dest": dest})
        with self.assertRaises(client.ApiError):
            self.c.call("POST", "/v1/backup", {"dest": dest})


class TestC6Quarantine(unittest.TestCase):
    def test_broken_chain_quarantines_and_owner_releases(self):
        h = TempHome().init()
        try:
            h.start()
            h.stop()
            s = Store(os.path.join(h.home, "imperium.db"))
            with s.tx() as conn:
                conn.execute("UPDATE events SET data='{\"x\":1}' WHERE seq=1")
            s.close()
            h.start()
            c = h.client()
            st = c.call("GET", "/v1/status")
            self.assertTrue(st["quarantine"])
            r = c.call("POST", "/v1/events_since", {"consumer": "owner"})  # reading stays possible
            self.assertTrue(r["ok"])
            for path, body in [("/v1/prune", {"through": 1}), ("/v1/token/rotate", {}),
                               ("/v1/builders", {"name": "x", "endpoint": "http://127.0.0.1:1", "session_id": "s",
                                                 "directory": "/x", "check": False})]:
                with self.assertRaises(client.ApiError) as e:
                    c.call("POST", path, body)
                self.assertEqual(e.exception.status, 503, path)
            c.call("POST", "/v1/quarantine/release", {"reason": "edited by me while testing"})
            st = c.call("GET", "/v1/status")
            self.assertFalse(st["quarantine"])
            v = c.call("GET", "/v1/verify-journal")
            self.assertTrue(v["chain_ok"])
            self.assertEqual(len(v["accepted_breaks"]), 1)
        finally:
            h.cleanup()


class TestC7RestoreValidation(StoreBase):
    def test_restore_refuses_a_backup_with_a_broken_chain(self):
        self.append()
        self.append()
        dest = os.path.join(self.home, "b.db")
        backup.backup(self.store, dest)
        conn = sqlite3.connect(dest)
        conn.execute("UPDATE events SET type='Z' WHERE seq=1")
        conn.commit()
        conn.close()
        self.store.close()
        with self.assertRaises(backup.RestoreError) as e:
            backup.restore(dest, os.path.join(self.home, "imperium.db"))
        self.assertIn("chain", str(e.exception))
        self.store = Store(os.path.join(self.home, "imperium.db"))

    def test_restore_refuses_inconsistent_consumers(self):
        with self.store.tx() as c:
            feeds.create(c, "d", principal="p", floor="ACTION")
        self.append()
        dest = os.path.join(self.home, "b.db")
        backup.backup(self.store, dest)
        conn = sqlite3.connect(dest)
        conn.execute("UPDATE consumers SET acked_seq=50")
        conn.commit()
        conn.close()
        self.store.close()
        with self.assertRaises(backup.RestoreError):
            backup.restore(dest, os.path.join(self.home, "imperium.db"))
        self.store = Store(os.path.join(self.home, "imperium.db"))


class TestC9Director(DaemonBase):
    def test_owner_registers_the_session_and_the_director_works(self):
        env = {"CLAUDE_CODE_SESSION_ID": "sess-1"}
        rc, body = run(self.h.home, "--as", "owner", "director", "claim", env=env)
        self.assertEqual(rc, 0, body)
        rc, body = run(self.h.home, "status", env=env)
        self.assertEqual(rc, 0, body)
        self.assertEqual(body["principal"], "director:sess-1")
        rc, body = run(self.h.home, "events", env=env)  # its own feed exists
        self.assertEqual(rc, 0, body)
        self.assertEqual(body["consumer"], "director")
        rc, body = run(self.h.home, "director", "show")
        self.assertEqual(body["director"]["session_id"], "sess-1")

    def test_claim_needs_a_claude_code_session(self):
        rc, body = run(self.h.home, "--as", "owner", "director", "claim", env={})
        self.assertEqual(rc, 1)
        self.assertIn("CLAUDE_CODE_SESSION_ID", body["error"])

    def test_second_session_cannot_take_over_without_release(self):
        run(self.h.home, "--as", "owner", "director", "claim", env={"CLAUDE_CODE_SESSION_ID": "s1"})
        rc, body = run(self.h.home, "--as", "owner", "director", "claim", env={"CLAUDE_CODE_SESSION_ID": "s2"})
        self.assertEqual(rc, 4)
        rc, body = run(self.h.home, "director", "release", env={"CLAUDE_CODE_SESSION_ID": "s1"})
        self.assertEqual(rc, 0, body)
        rc, body = run(self.h.home, "status", env={"CLAUDE_CODE_SESSION_ID": "s1"})
        self.assertEqual(rc, 4)  # the released token no longer works
        rc, body = run(self.h.home, "--as", "owner", "director", "claim", env={"CLAUDE_CODE_SESSION_ID": "s2"})
        self.assertEqual(rc, 0, body)


class TestC16ChainStatus(DaemonBase):
    def test_status_names_when_the_chain_was_checked_and_verify_refreshes_it(self):
        st = self.c.call("GET", "/v1/status")
        self.assertIn("checked_at", st["chain"])
        self.assertIn("checked_through_seq", st["chain"])
        before = st["chain"]["checked_at"]
        self.c.call("GET", "/v1/verify-journal")
        self.assertGreaterEqual(self.c.call("GET", "/v1/status")["chain"]["checked_at"], before)


if __name__ == "__main__":
    unittest.main()
