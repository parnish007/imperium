"""Retention: prefix-only pruning, the archive protocol and the start-up archive check (DESIGN §5, P2-21)."""
import gzip
import json
import os
import tempfile
import unittest
from unittest import mock

from imperium import feeds, journal, retention
from imperium.store import Store


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.archive_dir = os.path.join(self.home, "archive")
        self.store = Store(os.path.join(self.home, "imperium.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def append(self, severity="INFO", type_="X"):
        with self.store.tx() as c:
            return journal.append(c, type_, severity)

    def prune(self, through):
        return retention.prune(self.store, self.archive_dir, through)

    def count(self):
        with self.store.read() as c:
            return c.execute("SELECT COUNT(*) FROM events").fetchone()[0]


class TestPrune(Base):
    def test_prune_prefix_keeps_chain_verifiable(self):
        for _ in range(10):
            self.append()
        res = self.prune(6)
        self.assertEqual(res["boundary_seq"], 6)
        with self.store.read() as c:
            seqs = [r[0] for r in c.execute("SELECT seq FROM events ORDER BY seq")]
            self.assertEqual(seqs, [7, 8, 9, 10, 11])  # 11 = JOURNAL_PRUNED
            self.assertEqual(journal.get(c, 11)["type"], "JOURNAL_PRUNED")
            self.assertTrue(journal.verify_chain(c).ok)

    def test_archive_holds_the_pruned_rows_with_hashes(self):
        for _ in range(4):
            self.append()
        with self.store.read() as c:
            want = [journal.get(c, s)["hash"] for s in (1, 2, 3)]
        res = self.prune(3)
        with gzip.open(os.path.join(self.archive_dir, res["archive"]), "rt", encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        self.assertEqual([r["hash"] for r in rows], want)
        self.assertEqual(retention.sha256_file(os.path.join(self.archive_dir, res["archive"])),
                         res["archive_sha256"])

    def test_second_prune_verifies_from_new_boundary(self):
        for _ in range(10):
            self.append()
        self.prune(4)
        self.prune(8)
        with self.store.read() as c:
            self.assertTrue(journal.verify_chain(c).ok)

    def test_never_past_a_bookmark(self):
        for _ in range(6):
            self.append("ACTION")
        with self.store.tx() as c:
            feeds.create(c, "director", principal="director:d1", floor="ACTION")
            feeds.events_since(c, "director", "director:d1", limit=3)
            feeds.ack(c, "director", "director:d1", 3)
        with self.assertRaises(retention.PruneRefused):
            self.prune(4)
        self.prune(3)

    def test_never_past_unacknowledged_action_without_consumers(self):
        self.append()
        self.append("ACTION")
        self.append()
        with self.assertRaises(retention.PruneRefused):
            self.prune(2)
        self.prune(1)

    def test_critical_not_acked_by_a_feed_that_shows_it(self):
        self.append("CRITICAL")
        self.append()
        with self.store.tx() as c:
            feeds.create(c, "owner-notices", principal="owner", floor="NOTICE")
            feeds.create(c, "director", principal="director:d1", floor="CRITICAL")
            feeds.events_since(c, "owner-notices", "owner", limit=10)
            feeds.ack(c, "owner-notices", "owner", 2)
        with self.assertRaises(retention.PruneRefused):  # the director never acked it
            self.prune(1)

    def test_beyond_head_refused(self):
        self.append()
        with self.assertRaises(retention.PruneRefused):
            self.prune(5)


class TestArchiveProtocol(Base):
    def test_crash_before_transaction_leaves_rows_and_check_removes_orphan(self):
        for _ in range(5):
            self.append()
        with mock.patch.object(retention, "_commit_prune", side_effect=RuntimeError("power cut")):
            with self.assertRaises(RuntimeError):
                self.prune(3)
        self.assertEqual(self.count(), 5)
        self.assertEqual(len(os.listdir(self.archive_dir)), 1)
        report = retention.check_archives(self.store, self.archive_dir)
        self.assertEqual(len(report["removed"]), 1)
        self.assertEqual(os.listdir(self.archive_dir), [])
        with self.store.read() as c:
            self.assertEqual(journal.get(c, 6)["type"], "ARCHIVE_ORPHAN_REMOVED")

    def test_crash_before_rename_leaves_tmp_which_check_removes(self):
        for _ in range(5):
            self.append()
        with mock.patch.object(retention.os, "replace", side_effect=RuntimeError("power cut")):
            with self.assertRaises(RuntimeError):
                self.prune(3)
        self.assertTrue(any(n.endswith(".tmp") for n in os.listdir(self.archive_dir)))
        retention.check_archives(self.store, self.archive_dir)
        self.assertEqual(os.listdir(self.archive_dir), [])
        self.assertEqual(self.count(), 6)  # 5 + the NOTICE

    def test_missing_archive_is_critical(self):
        for _ in range(5):
            self.append()
        res = self.prune(3)
        os.remove(os.path.join(self.archive_dir, res["archive"]))
        report = retention.check_archives(self.store, self.archive_dir)
        self.assertEqual(report["missing"], [res["archive"]])
        with self.store.read() as c:
            last = c.execute("SELECT MAX(seq) FROM events").fetchone()[0]
            ev = journal.get(c, last)
        self.assertEqual((ev["type"], ev["severity"]), ("ARCHIVE_MISSING", "CRITICAL"))

    def test_altered_archive_is_critical(self):
        for _ in range(5):
            self.append()
        res = self.prune(3)
        with open(os.path.join(self.archive_dir, res["archive"]), "ab") as f:
            f.write(b"x")
        report = retention.check_archives(self.store, self.archive_dir)
        self.assertEqual(report["mismatched"], [res["archive"]])

    def test_clean_check_reports_nothing(self):
        for _ in range(5):
            self.append()
        self.prune(3)
        report = retention.check_archives(self.store, self.archive_dir)
        self.assertEqual((report["removed"], report["missing"], report["mismatched"]), ([], [], []))


if __name__ == "__main__":
    unittest.main()
