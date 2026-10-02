"""CLI: init, status, doctor, events/ack/show, verify-journal, prune, backup/restore, up/down (DESIGN §11.1)."""
import io
import json
import os
import pathlib
import time
import unittest

from imperium import cli, journal, tokens
from imperium.store import Store

from helpers import TempHome


def run(home, *args, as_owner=False):
    out, err = io.StringIO(), io.StringIO()
    argv = ["--home", home, "--json"] + (["--as", "owner"] if as_owner else []) + list(args)
    rc = cli.main(argv, out=out, err=err, env={})
    text = out.getvalue()
    return rc, (json.loads(text) if text.strip() else None), err.getvalue()


class TestOffline(unittest.TestCase):
    def setUp(self):
        self.h = TempHome().init()

    def tearDown(self):
        self.h.cleanup()

    def test_init_is_idempotent(self):
        first = tokens.read_locator(self.h.home, "owner")
        rc, body, _ = run(self.h.home, "init")
        self.assertEqual(rc, 0)
        self.assertEqual(tokens.read_locator(self.h.home, "owner"), first)
        self.assertTrue(os.path.exists(os.path.join(self.h.home, "imperium.toml")))

    def test_status_when_daemon_down_exits_3(self):
        rc, body, _ = run(self.h.home, "status")
        self.assertEqual(rc, 3)
        self.assertFalse(body["ok"])

    def test_doctor_offline_has_no_failures(self):
        rc, body, _ = run(self.h.home, "doctor")
        self.assertEqual(rc, 0, body)
        names = {c["name"]: c["status"] for c in body["checks"]}
        self.assertEqual(names["python"], "OK")
        self.assertEqual(names["journal chain"], "OK")
        self.assertEqual(names["daemon"], "WARN")

    def test_verify_journal_offline_detects_tampering(self):
        rc, body, _ = run(self.h.home, "verify-journal")
        self.assertEqual(rc, 0)
        s = Store(os.path.join(self.h.home, "imperium.db"))
        with s.tx() as conn:
            journal.append(conn, "X", "INFO")
            journal.append(conn, "X", "INFO")
            conn.execute("UPDATE events SET type='Y' WHERE seq=1")
        s.close()
        rc, body, _ = run(self.h.home, "verify-journal")
        self.assertEqual(rc, 5)
        self.assertEqual(body["first_bad"], 1)

    def test_bad_config_is_reported_not_a_traceback(self):
        with open(os.path.join(self.h.home, "imperium.toml"), "w", encoding="utf-8") as f:
            f.write("[daemon]\nunknown_key = 1\n")
        rc, body, err = run(self.h.home, "doctor")
        self.assertEqual(rc, 5)
        cfg = [c for c in body["checks"] if c["name"] == "config"][0]
        self.assertEqual(cfg["status"], "FAIL")
        self.assertIn("unknown_key", cfg["detail"])
        self.assertNotIn("Traceback", err)

    def test_usage_error_exits_2(self):
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(cli.main(["--home", self.h.home, "frobnicate"], out=out, err=err, env={}), 2)


class TestOnline(unittest.TestCase):
    def setUp(self):
        self.h = TempHome().init().start()

    def tearDown(self):
        self.h.cleanup()

    def append(self, severity):
        with self.h.d.store.tx() as conn:
            return journal.append(conn, "X", severity)

    def test_init_refused_while_daemon_runs(self):
        rc, body, _ = run(self.h.home, "init")
        self.assertEqual(rc, 4)

    def test_status(self):
        rc, body, _ = run(self.h.home, "status")
        self.assertEqual(rc, 0)
        self.assertEqual(body["run_id"], self.h.d.run_id)
        self.assertIn("needs_you", body)

    def test_events_ack_show(self):
        a = self.append("ACTION")
        rc, body, _ = run(self.h.home, "events")
        self.assertEqual(rc, 0)
        self.assertIn(a, [e["seq"] for e in body["events"]])
        rc, body, _ = run(self.h.home, "show", str(a))
        self.assertEqual(body["event"]["seq"], a)
        rc, body, _ = run(self.h.home, "ack", str(a))
        self.assertEqual(rc, 0)

    def test_ack_unshown_is_refused_with_4(self):
        a = self.append("ACTION")
        rc, body, _ = run(self.h.home, "ack", str(a))
        self.assertEqual(rc, 4)

    def test_events_drains_to_high_water_across_pages(self):
        seqs = [self.append("NOTICE") for _ in range(7)]
        rc, body, _ = run(self.h.home, "events", "--page-size", "2")
        got = [e["seq"] for e in body["events"]]
        self.assertTrue(set(seqs) <= set(got))
        self.assertEqual(got, sorted(got))

    def test_prune_and_verify(self):
        for _ in range(5):
            self.append("INFO")
        run(self.h.home, "events")
        with self.h.d.store.read() as conn:
            hw = journal.head(conn)[0]
        run(self.h.home, "ack", str(hw))
        rc, body, _ = run(self.h.home, "prune", "--through", "3")
        self.assertEqual(rc, 0, body)
        rc, body, _ = run(self.h.home, "verify-journal")
        self.assertEqual(rc, 0)
        self.assertTrue(body["ok"])

    def test_backup_restore_observe_only(self):
        rc, body, _ = run(self.h.home, "backup")
        self.assertEqual(rc, 0, body)
        path = body["path"]
        self.assertTrue(os.path.exists(path))
        rc, _, _ = run(self.h.home, "restore", path)
        self.assertEqual(rc, 4)  # refused while the daemon runs
        self.h.stop()
        rc, body, _ = run(self.h.home, "restore", path)
        self.assertEqual(rc, 0, body)
        self.h.start()
        rc, body, _ = run(self.h.home, "status")
        self.assertEqual(body["observe_only"], "restored")
        rc, body, _ = run(self.h.home, "restore-confirm")
        self.assertEqual(rc, 0)
        rc, body, _ = run(self.h.home, "status")
        self.assertIsNone(body["observe_only"])


class TestUpDown(unittest.TestCase):
    """Starts a real detached daemon process (about 20 MB) and stops it."""

    def test_up_status_down(self):
        h = TempHome().init()
        env_backup = os.environ.get("PYTHONPATH")
        src = str(pathlib.Path(__file__).resolve().parents[1] / "src")
        os.environ["PYTHONPATH"] = src + (os.pathsep + env_backup if env_backup else "")
        try:
            rc, body, err = run(h.home, "up")
            self.assertEqual(rc, 0, (body, err))
            rc, body2, _ = run(h.home, "up")
            self.assertEqual(rc, 0)
            self.assertEqual(body2["run_id"], body["run_id"])  # idempotent
            rc, st, _ = run(h.home, "status")
            self.assertEqual(rc, 0)
            rc, body, _ = run(h.home, "down")
            self.assertEqual(rc, 0, body)
            rc, _, _ = run(h.home, "status")
            self.assertEqual(rc, 3)
        finally:
            run(h.home, "down")
            if env_backup is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = env_backup
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    h.cleanup()
                    break
                except (PermissionError, OSError):
                    time.sleep(0.2)


if __name__ == "__main__":
    unittest.main()
