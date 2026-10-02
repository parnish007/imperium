"""Backup and restore (P2-12), tokens and credential locators (P2-14), capped call audit (P2-13)."""
import os
import tempfile
import unittest

from imperium import audit, backup, journal, tokens
from imperium.store import Store


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.db = os.path.join(self.home, "imperium.db")
        self.store = Store(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def append(self, n=1):
        for _ in range(n):
            with self.store.tx() as c:
                journal.append(c, "X", "INFO")


class TestBackup(Base):
    def test_backup_contains_wal_committed_rows(self):
        self.append(5)  # committed, still in the WAL (no checkpoint yet)
        dest = os.path.join(self.home, "b.db")
        backup.backup(self.store, dest)
        copy = Store(dest)
        try:
            with copy.read() as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM events").fetchone()[0], 5)
                self.assertTrue(journal.verify_chain(c).ok)
        finally:
            copy.close()

    def test_restore_sets_observe_only_and_records_it(self):
        self.append(2)
        dest = os.path.join(self.home, "b.db")
        backup.backup(self.store, dest)
        self.append(3)  # these are forgotten by the restore
        self.store.close()
        backup.restore(dest, self.db)
        self.store = Store(self.db)
        with self.store.read() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM events").fetchone()[0], 3)  # 2 + RESTORED
            last = journal.get(c, 3)
            self.assertEqual((last["type"], last["severity"]), ("RESTORED", "ACTION"))
            self.assertTrue(journal.verify_chain(c).ok)
        self.assertEqual(backup.observe_only(self.store), "restored")
        backup.confirm_restore(self.store, by="owner")
        self.assertIsNone(backup.observe_only(self.store))

    def test_restore_refuses_a_non_imperium_file(self):
        bad = os.path.join(self.home, "bad.db")
        with open(bad, "wb") as f:
            f.write(b"not a database")
        self.store.close()
        with self.assertRaises(backup.RestoreError):
            backup.restore(bad, self.db)
        self.store = Store(self.db)


class TestTokens(Base):
    def test_issue_verify_revoke(self):
        with self.store.tx() as c:
            raw = tokens.issue(c, "owner")
        self.assertGreaterEqual(len(raw), 43)  # 256 bits, url-safe base64
        with self.store.read() as c:
            self.assertEqual(tokens.verify(c, raw), "owner")
            self.assertIsNone(tokens.verify(c, raw + "x"))
            stored = [r[0] for r in c.execute("SELECT hash FROM tokens")]
        self.assertNotIn(raw, stored)  # stored hashed
        with self.store.tx() as c:
            tokens.revoke_principal(c, "owner")
        with self.store.read() as c:
            self.assertIsNone(tokens.verify(c, raw))

    def test_locator_files(self):
        tokens.write_locator(self.home, "owner", "abc")
        tokens.write_locator(self.home, "director:sess-1", "def")
        self.assertEqual(tokens.read_locator(self.home, "owner"), "abc")
        self.assertEqual(tokens.read_locator(self.home, "director:sess-1"), "def")
        self.assertIsNone(tokens.read_locator(self.home, "director:sess-2"))
        if os.name == "posix":
            mode = os.stat(os.path.join(self.home, "tokens", "owner")).st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_principal_selection_never_falls_back(self):
        tokens.write_locator(self.home, "owner", "OWN")
        tokens.write_locator(self.home, "director:s1", "DIR")
        pick = tokens.select_token
        self.assertEqual(pick(self.home, env={}, as_owner=False), ("owner", "OWN"))
        self.assertEqual(pick(self.home, env={"CLAUDE_CODE_SESSION_ID": "s1"}, as_owner=False), ("director:s1", "DIR"))
        self.assertEqual(pick(self.home, env={"CLAUDE_CODE_SESSION_ID": "s1"}, as_owner=True), ("owner", "OWN"))
        with self.assertRaises(tokens.NoCredential):  # inside Claude Code, no director token: no owner fallback
            pick(self.home, env={"CLAUDE_CODE_SESSION_ID": "s2"}, as_owner=False)

    def test_session_id_cannot_escape_tokens_dir(self):
        with self.assertRaises(ValueError):
            tokens.write_locator(self.home, "director:../../evil", "x")


class TestAudit(Base):
    def test_cap_drops_oldest_and_counts(self):
        with self.store.tx() as c:
            for i in range(25):
                audit.record(c, "owner", None, f"/v1/r{i}", "ok", cap=10)
        with self.store.read() as c:
            rows = [r[0] for r in c.execute("SELECT route FROM call_audit ORDER BY id")]
            self.assertEqual(rows, [f"/v1/r{i}" for i in range(15, 25)])
            self.assertEqual(audit.dropped(c), 15)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)  # never the journal


if __name__ == "__main__":
    unittest.main()
