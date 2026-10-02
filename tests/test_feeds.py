"""Feeds: fixed floor, strict order, ack refused beyond what was shown, bounded drains (DESIGN §8.1)."""
import os
import random
import tempfile
import unittest

from imperium import feeds, journal
from imperium.store import Store


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "imperium.db"))
        with self.store.tx() as c:
            feeds.create(c, "director", principal="director:d1", floor="ACTION")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def append(self, severity, type_="X"):
        with self.store.tx() as c:
            return journal.append(c, type_, severity)

    def page(self, limit=10, after=None, high_water=None, consumer="director", principal="director:d1"):
        with self.store.tx() as c:
            return feeds.events_since(c, consumer, principal, limit=limit, after=after, high_water=high_water)

    def ack(self, seq, consumer="director", principal="director:d1"):
        with self.store.tx() as c:
            return feeds.ack(c, consumer, principal, seq)


class TestFeeds(Base):
    def test_floor_filters_and_order(self):
        seqs = [self.append(s) for s in ["INFO", "ACTION", "DEBUG", "CRITICAL", "NOTICE", "ACTION"]]
        r = self.page()
        self.assertEqual([e["seq"] for e in r["events"]], [seqs[1], seqs[3], seqs[5]])

    def test_floor_is_fixed(self):
        with self.store.tx() as c:
            with self.assertRaises(feeds.FeedError):
                feeds.create(c, "director", principal="director:d1", floor="INFO")

    def test_ack_beyond_shown_refused(self):
        a = self.append("ACTION")
        b = self.append("ACTION")
        with self.assertRaises(feeds.Refused):
            self.ack(b)  # nothing shown yet
        self.page(limit=1)  # shows a only
        self.ack(a)
        with self.assertRaises(feeds.Refused):
            self.ack(b)

    def test_ack_after_full_page_covers_events_below_floor(self):
        self.append("ACTION")
        self.append("INFO")
        r = self.page()
        self.ack(r["high_water"])  # INFO after the last ACTION was never in the feed
        with self.store.read() as c:
            self.assertEqual(feeds.get(c, "director")["acked_seq"], r["high_water"])

    def test_paging_with_after_does_not_skip(self):
        seqs = [self.append("ACTION") for _ in range(5)]
        r1 = self.page(limit=2)
        r2 = self.page(limit=2, after=r1["events"][-1]["seq"], high_water=r1["high_water"])
        r3 = self.page(limit=2, after=r2["events"][-1]["seq"], high_water=r1["high_water"])
        got = [e["seq"] for r in (r1, r2, r3) for e in r["events"]]
        self.assertEqual(got, seqs)
        self.ack(seqs[-1])

    def test_jumping_ahead_with_after_does_not_extend_shown(self):
        seqs = [self.append("ACTION") for _ in range(4)]
        self.page(limit=1, after=seqs[1])  # skipped seqs[0], seqs[1]
        with self.assertRaises(feeds.Refused):
            self.ack(seqs[2])

    def test_drain_ends_at_high_water_while_events_keep_arriving(self):
        for _ in range(3):
            self.append("ACTION")
        r = self.page(limit=1)
        hw = r["high_water"]
        pages = 1
        after = r["events"][-1]["seq"]
        while True:
            self.append("ACTION")  # a busy builder keeps producing
            r = self.page(limit=1, after=after, high_water=hw)
            if not r["events"]:
                break
            after = r["events"][-1]["seq"]
            pages += 1
            self.assertLess(pages, 10)
        self.assertEqual(pages, 3)

    def test_urgent_summary(self):
        self.append("ACTION")
        c1 = self.append("CRITICAL")
        c2 = self.append("CRITICAL")
        r = self.page(limit=1)
        self.assertEqual(r["urgent"]["count"], 2)
        self.assertEqual([u["seq"] for u in r["urgent"]["items"]], [c1, c2])
        self.page(limit=10)
        self.ack(c1)
        r = self.page(limit=1)
        self.assertEqual(r["urgent"]["count"], 1)

    def test_show_does_not_move_anything(self):
        a = self.append("ACTION")
        with self.store.read() as c:
            ev = feeds.show(c, a)
            st = feeds.get(c, "director")
        self.assertEqual(ev["seq"], a)
        self.assertEqual(st["shown_through"], 0)
        with self.assertRaises(feeds.Refused):
            self.ack(a)

    def test_only_owning_principal_reads_or_acks(self):
        self.append("ACTION")
        with self.assertRaises(feeds.Refused):
            self.page(principal="owner")
        self.page()
        with self.assertRaises(feeds.Refused):
            self.ack(1, principal="director:other")

    def test_ack_is_monotonic(self):
        seqs = [self.append("ACTION") for _ in range(3)]
        self.page()
        self.ack(seqs[2])
        self.ack(seqs[0])  # older ack is a no-op
        with self.store.read() as c:
            self.assertEqual(feeds.get(c, "director")["acked_seq"], seqs[2])

    def test_randomised_never_acked_unshown(self):
        """Random reads, peeks and acks: every feed event at or below acked_seq was returned by a page."""
        rng = random.Random(7)
        returned = set()
        for _ in range(400):
            op = rng.random()
            if op < 0.4:
                self.append(rng.choice(["DEBUG", "INFO", "NOTICE", "ACTION", "CRITICAL"]))
            elif op < 0.7:
                after = rng.choice([None, None, rng.randint(0, 50)])
                r = self.page(limit=rng.randint(1, 4), after=after)
                returned.update(e["seq"] for e in r["events"])
            else:
                with self.store.read() as c:
                    st = feeds.get(c, "director")
                target = rng.randint(0, st["shown_through"] + 3)
                try:
                    self.ack(target)
                except feeds.Refused:
                    self.assertGreater(target, st["shown_through"])
        with self.store.read() as c:
            acked = feeds.get(c, "director")["acked_seq"]
            in_feed = [r[0] for r in c.execute(
                "SELECT seq FROM events WHERE severity >= 3 AND seq <= ?", (acked,))]
        self.assertTrue(set(in_feed) <= returned)


if __name__ == "__main__":
    unittest.main()
