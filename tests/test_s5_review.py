"""Regression tests for the stage 5 cross-family review (Codex): each test failed before its fix."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from imperium import approvals, journal, outbox, rounds, snapshot, verify
from imperium.store import meta_set
from test_approvals import Base as ApprovalBase
from test_approvals import run as arun
from test_rounds import PY, Base as RoundBase
from test_rounds import git, run


class TestVerification(RoundBase):
    def verified(self):
        self.standing_check()
        rid, nonce = self.open()
        self.fix()
        self.report(rid, nonce)
        self.c.call("POST", "/v1/rounds/objective", {"id": rid, "met": True})
        r = self.verify(rid)
        self.assertEqual(r["state"], "VERIFIED")
        return rid, nonce

    def test_c1_verifying_again_voids_verified_and_a_failing_candidate_cannot_be_accepted(self):
        rid, _ = self.verified()
        self.write("calc.py", "def add(a, b):\n    return a - b\n")
        r = self.verify(rid)
        self.assertNotEqual(r["state"], "VERIFIED")
        self.assertIn("VERIFICATION_VOIDED", self.types())
        with self.assertRaises(Exception):
            self.c.call("POST", "/v1/rounds/decide", {"id": rid, "decision": "accept"})
        self.assertNotEqual(self.round(rid)["state"], "ACCEPTED")

    def test_c1_accept_is_refused_while_a_verification_runs(self):
        rid, _ = self.verified()
        with self.h.d.store.tx() as conn:
            conn.execute("UPDATE rounds SET verify_job='j1' WHERE id=?", (rid,))
        with self.assertRaises(Exception):
            self.c.call("POST", "/v1/rounds/decide", {"id": rid, "decision": "accept"})

    def test_c3_a_new_required_check_voids_earlier_results(self):
        self.standing_check()
        rid, nonce = self.open()
        self.fix()
        self.report(rid, nonce)
        self.assertEqual(self.verify(rid)["checks_ok_generation"], 1)  # checks pass before the objective
        self.c.call("POST", "/v1/checks", {"id": "strict", "builder": "coding", "argv": [PY, "-c", "raise SystemExit(1)"],
                                           "timeout": 60})
        self.c.call("POST", "/v1/rounds/objective", {"id": rid, "met": True})
        self.assertNotEqual(self.round(rid)["state"], "VERIFIED")

    def test_c3_defining_a_check_voids_a_verified_round(self):
        rid, _ = self.verified()
        self.c.call("POST", "/v1/checks", {"id": "lint", "builder": "coding", "argv": [PY, "-c", "pass"],
                                           "timeout": 60})
        self.assertEqual(self.round(rid)["state"], "CLAIMED_READY")
        self.assertIn("VERIFICATION_VOIDED", self.types())

    def test_c3_results_of_a_run_during_which_the_checks_changed_are_not_used(self):
        self.standing_check()
        rid, nonce = self.open()
        self.fix()
        self.report(rid, nonce)
        self.c.call("POST", "/v1/rounds/objective", {"id": rid, "met": True})
        real = self.h.d.verifier._run_on

        def run_on(*a, **kw):  # a failing required check is defined while the others run
            res = real(*a, **kw)
            if not any(c["id"] == "strict" for c in self.c.call("GET", "/v1/checks")["checks"]):
                self.c.call("POST", "/v1/checks", {"id": "strict", "builder": "coding",
                                                   "argv": [PY, "-c", "raise SystemExit(1)"], "timeout": 60})
            return res

        self.h.d.verifier._run_on = run_on
        r = self.verify(rid)
        self.assertNotEqual(r["state"], "VERIFIED")
        self.assertIsNone(r["checks_ok_generation"])

    def test_c15_results_are_not_published_from_a_quarantined_journal(self):
        self.standing_check()
        rid, nonce = self.open()
        self.fix()
        self.report(rid, nonce)
        self.c.call("POST", "/v1/rounds/objective", {"id": rid, "met": True})
        real = self.h.d.verifier._run_on

        def run_on(*a, **kw):
            res = real(*a, **kw)
            with self.h.d.store.tx() as conn:
                meta_set(conn, "quarantine", "test: chain broken while the check ran")
            return res

        self.h.d.verifier._run_on = run_on
        r = self.verify(rid)
        self.assertNotEqual(r["state"], "VERIFIED")
        self.assertIsNone(r["checks_ok_generation"])

    def test_c4_accept_is_refused_while_the_builder_works(self):
        rid, _ = self.verified()
        self.fake.set_status("ses_r1", "busy")
        self.cycle()
        with self.assertRaises(Exception) as e:
            self.c.call("POST", "/v1/rounds/decide", {"id": rid, "decision": "accept"})
        self.assertIn("working", str(e.exception))
        with self.h.d.store.read() as conn:
            self.assertIsNone(outbox.holder(conn, "coding"))  # the reservation was released

    def test_c14_a_decision_cancels_every_queued_message_and_none_is_sent_into_a_decided_round(self):
        self.standing_check()
        rid, _ = self.open()
        with self.h.d.store.tx() as conn:
            for i in range(205):
                outbox.enqueue(conn, builder="coding", body=f"m{i}", client_key=f"bulk{i}", principal="owner",
                               kind="round", round_=rid, now=self.now)
        self.c.call("POST", "/v1/rounds/decide", {"id": rid, "decision": "reject"})
        with self.h.d.store.read() as conn:
            left = conn.execute("SELECT COUNT(*) FROM outbox WHERE round=? AND state='QUEUED'", (rid,)).fetchone()[0]
            # and a message that slips in afterwards is cancelled at dispatch, never sent
        self.assertEqual(left, 0)
        with self.h.d.store.tx() as conn:
            m = outbox.enqueue(conn, builder="coding", body="late", client_key="late", principal="owner",
                               kind="round", round_=rid, now=self.now)
        self.cycle(3)
        self.assertEqual(self.c.call("GET", f"/v1/message?id={m['id']}")["message"]["state"], "CANCELLED")


class TestRecovery(RoundBase):
    def test_c2_a_daemon_stopped_during_verification_starts_again(self):
        self.standing_check()
        rid, _ = self.open()
        with self.h.d.store.tx() as conn:
            conn.execute("UPDATE rounds SET verify_job='j1' WHERE id=?", (rid,))
            outbox.reserve(conn, "coding", f"verify:{rid}", self.now)
        self.h.stop()
        self.h.start()  # failed with IndexError before the fix, every time
        self.assertIn("VERIFY_INTERRUPTED", self.types())
        self.assertIsNone(self.round(rid)["verify_job"])
        with self.h.d.store.read() as conn:
            self.assertIsNone(outbox.holder(conn, "coding"))


class TestBrake(ApprovalBase):
    def test_c5_a_decided_approval_is_not_sent_under_stop_all(self):
        aid = self.ask()
        with self.h.d.store.tx() as conn:  # decided, reply not yet sent
            approvals.decide(conn, aid, "once", "owner", None, time.time())
        self.c.call("POST", "/v1/stop-all", {})
        self.cycle(2)
        self.assertEqual(self.fake.permission_replies, [])
        self.c.call("POST", "/v1/resume-all", {})
        self.cycle(2)
        self.assertEqual(self.fake.permission_replies, [(aid, {"reply": "once"})])

    def test_c5_a_reject_still_goes_out_under_stop_all(self):
        aid = self.ask()
        with self.h.d.store.tx() as conn:
            approvals.decide(conn, aid, "reject", "owner", None, time.time())
        self.c.call("POST", "/v1/stop-all", {})
        self.cycle(2)
        self.assertEqual(self.fake.permission_replies, [(aid, {"reply": "reject"})])

    def test_c5_stop_all_committed_during_the_cycle_stops_the_dispatch(self):
        m = self.c.call("POST", "/v1/send", {"builder": "coding", "body": "go", "client_key": "k"})["message"]
        eng = self.h.d.engine
        real = eng._base_snapshot

        def braked(b):  # stop-all lands after the cycle read it and before the dispatch transaction
            real(b)
            with self.h.d.store.tx() as conn:
                meta_set(conn, "stop_all", "test")

        eng._base_snapshot = braked
        self.cycle()
        self.assertEqual(self.c.call("GET", f"/v1/message?id={m['id']}")["message"]["state"], "QUEUED")

    def test_c6_an_ask_with_cut_patterns_is_never_allowed_by_a_rule(self):
        self.rule()
        d = self.director()
        d.call("POST", "/v1/director/presence", {})
        aid = self.ask(patterns=["git status"] * 50 + ["rm -rf /important"])
        self.assertEqual(self.approval(aid)["state"], "PENDING")
        aid2 = self.ask(patterns=["git status " + "x" * 2100 + " ; rm -rf /important"])
        self.assertEqual(self.approval(aid2)["state"], "PENDING")
        aid3 = self.ask(patterns=["git status"])  # an ordinary ask is still answered
        self.assertEqual(self.approval(aid3)["state"], "APPROVED")

    def test_c10_the_report_marks_reviewed_only_what_it_showed(self):
        with self.h.d.store.tx() as conn:
            for i in range(5):
                seq = journal.append(conn, "APPROVAL_AUTO", "NOTICE", data={"i": i})
                conn.execute("INSERT INTO approvals(id, builder, permission, patterns, always, state, by_policy, "
                             "reply_state, created, decided_seq) VALUES(?,?,?,?,?,?,1,'sent',?,?)",
                             (f"per_{i}", "coding" if i % 2 else "other", "bash", "[]", "[]", "APPROVED", i, seq))
        page = self.c.call("GET", "/v1/approvals/auto?limit=2")
        self.assertEqual(len(page["approvals"]), 2)
        self.assertTrue(page["more"])
        self.c.call("POST", "/v1/approvals/report-seen", {"through": page["through"]})
        self.assertEqual(len(self.c.call("GET", "/v1/approvals/auto")["approvals"]), 3)
        rc, body = arun(self.h.home, "approvals", "--auto", "--builder", "coding")  # filtered: marks nothing
        self.assertEqual(rc, 0, body)
        self.assertFalse(body["marked_reviewed"])
        self.assertEqual(len(self.c.call("GET", "/v1/approvals/auto")["approvals"]), 3)
        rc, body = arun(self.h.home, "approvals", "--auto")
        self.assertEqual(len(body["approvals"]), 3)
        self.assertEqual(self.c.call("GET", "/v1/approvals/auto")["approvals"], [])

    def test_c11_export_contains_every_event_from_the_first(self):
        with self.h.d.store.tx() as conn:
            for i in range(1100):
                journal.append(conn, "FILLER", "INFO", data={"i": i})
            total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        out = os.path.join(self.h.tmp.name, "export.jsonl")
        rc, body = arun(self.h.home, "export", "--out", out)
        self.assertEqual(rc, 0, body)
        with open(out, encoding="utf-8") as f:
            seqs = [json.loads(line)["seq"] for line in f]
        self.assertEqual(seqs[0], 1)
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertGreaterEqual(len(seqs), total)


class TestPaths(unittest.TestCase):
    def test_c7_a_symlinked_parent_of_a_new_file_is_resolved(self):
        root = tempfile.mkdtemp()
        allowed, outside = os.path.join(root, "allowed"), os.path.join(root, "outside")
        os.makedirs(allowed)
        os.makedirs(outside)
        try:
            os.symlink(outside, os.path.join(allowed, "link"), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this account cannot create symlinks")
        self.assertFalse(approvals.path_within(os.path.join(allowed, "link", "new.py"), allowed))
        self.assertTrue(approvals.path_within(os.path.join(allowed, "real", "new.py"), allowed))


class TestChecks(unittest.TestCase):
    def test_c12_a_check_that_leaves_a_process_behind_still_ends(self):
        # The check starts a helper that starts a sleeper in its own session and exits at once: the sleeper is an
        # orphan holding the check's output, out of reach of a kill that follows parent links or process groups.
        tmp = tempfile.mkdtemp()
        pidfile = os.path.join(tmp, "sleeper.pid").replace("\\", "/")
        flags = "creationflags=0x00000008" if os.name == "nt" else "start_new_session=True"  # DETACHED_PROCESS
        helper = (f"import subprocess, sys; c = subprocess.Popen([sys.executable, '-c', 'import time; "
                  f"time.sleep(90)'], {flags}); open('{pidfile}', 'w').write(str(c.pid))")
        script = ("import subprocess, sys, time\n"
                  f"subprocess.run([sys.executable, '-c', {helper!r}])\n"
                  "time.sleep(90)\n")
        box = {}

        def go():
            box["res"] = verify.run_check([sys.executable, "-c", script], tmp, [], 3,
                                          os.path.join(tmp, "out", "o.txt"))

        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(60)
        self.assertFalse(t.is_alive(), "the check never ended: a leftover process kept its output open")
        self.assertTrue(box["res"]["timed_out"])
        if os.name == "nt":  # the job object took the orphan too
            with open(pidfile) as f:
                pid = f.read().strip()
            time.sleep(1)
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
            self.assertNotIn(pid, out)


class TestSubmodules(unittest.TestCase):
    def test_c13_a_repository_with_a_submodule_is_refused(self):
        ws = tempfile.mkdtemp()
        with open(os.path.join(ws, "a.txt"), "w") as f:
            f.write("a\n")
        git(ws, "init", "-q")
        git(ws, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
        git(ws, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        head = git(ws, "rev-parse", "HEAD")
        git(ws, "update-index", "--add", "--cacheinfo", f"160000,{head},lib")
        git(ws, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "sub")
        with self.assertRaises(snapshot.SnapshotError) as e:
            snapshot.take(ws, "refs/imperium/t/s")
        self.assertIn("submodule", str(e.exception))


if __name__ == "__main__":
    unittest.main()


# --- second and third reviewers (DeepSeek, muse) -------------------------------------------------------------

class TestGitInput(unittest.TestCase):
    def repo(self):
        ws = tempfile.mkdtemp()
        with open(os.path.join(ws, "a.txt"), "w") as f:
            f.write("a\n")
        git(ws, "init", "-q")
        git(ws, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
        git(ws, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        return ws

    @unittest.skipIf(os.name == "nt", "Windows file names cannot hold a line break")
    def test_d1_a_line_break_in_a_name_cannot_add_index_entries(self):
        ws = self.repo()
        blob = git(ws, "hash-object", "-w", "a.txt")
        # a name cannot hold "/", but it can name another top-level entry: here one the builder never wrote
        with open(os.path.join(ws, f"evil\n100644 {blob}\tinjected.txt"), "w") as f:
            f.write("x")
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.take(ws, "refs/imperium/t/nl")

    def test_d4_hooks_in_the_old_shared_folder_never_run(self):
        ws = self.repo()
        hooks = os.path.join(tempfile.gettempdir(), "imperium-no-hooks")
        os.makedirs(hooks, exist_ok=True)
        marker = os.path.join(tempfile.mkdtemp(), "PWNED").replace("\\", "/")
        hook = os.path.join(hooks, "reference-transaction")
        with open(hook, "w", newline="\n") as f:
            f.write(f"#!/bin/sh\necho x > '{marker}'\n")
        os.chmod(hook, 0o755)
        self.addCleanup(os.remove, hook)
        snapshot.take(ws, "refs/imperium/t/hook")
        self.assertFalse(os.path.exists(marker))


class TestRelativeExecutable(unittest.TestCase):
    def test_d6_a_relative_executable_is_hashed_from_the_checks_directory(self):
        d = tempfile.mkdtemp()
        script = os.path.join(d, "run.py")
        with open(script, "w") as f:
            f.write("print('hi')\n")
        res = verify.run_check([os.path.join(".", "run.py")], d, [], 10, os.path.join(d, "o", "o.txt"))
        import hashlib
        with open(script, "rb") as f:
            self.assertEqual(res["executable_sha256"], hashlib.sha256(f.read()).hexdigest())


class TestRoundRules(RoundBase):
    def test_d8_no_repair_before_the_brief_is_taken_in(self):
        self.standing_check()
        r = self.c.call("POST", "/v1/rounds", {"builder": "coding", "objective": "x", "client_key": "k"})["round"]
        with self.assertRaises(Exception):
            self.c.call("POST", "/v1/rounds/message", {"id": r["id"], "body": "fix", "client_key": "m1"})

    def test_d5_stale_reports_are_rate_limited(self):
        self.standing_check()
        rid, nonce = self.open()
        self.report(rid, nonce, gen=7)
        with self.assertRaises(Exception):  # within 10 s: refused, not journaled again
            self.b.call("POST", "/v1/builder/report", {"round": rid, "nonce": nonce, "generation": 7,
                                                       "state": "ready"})
        self.assertEqual(self.types().count("STALE_CLAIM"), 1)

    def test_d2_accept_needs_a_workspace_comparison(self):
        self.standing_check()
        rid, nonce = self.open()
        with self.h.d.store.tx() as conn:
            conn.execute("UPDATE rounds SET state='VERIFIED', cand_tree='t', cand_generation=1, "
                         "checks_ok_generation=1 WHERE id=?", (rid,))
            with self.assertRaises(rounds.Conflict):
                rounds.decide(conn, rid, "accept", "owner", "", self.now)

    def test_d7_depends_must_be_a_map(self):
        with self.h.d.store.tx() as conn:
            with self.assertRaises(rounds.RoundError):
                rounds.define_check(conn, cid="x", scope="builder:coding", argv=[PY], working_dir=".", env=[],
                                    timeout=10, must_fail_on_base=False, depends=[], required=True,
                                    principal="owner")


class TestApprovalMatching(ApprovalBase):
    def test_m3_an_ask_without_patterns_is_never_allowed_by_a_rule(self):
        self.rule(pattern="*")
        self.director().call("POST", "/v1/director/presence", {})
        aid = self.ask(patterns=[])
        self.assertEqual(self.approval(aid)["state"], "PENDING")

    @unittest.skipUnless(os.name == "nt", "Windows spelling")
    def test_m2_a_deny_rule_is_not_dodged_by_case_or_separators(self):
        self.rule(permission="*", pattern="*")
        self.rule(permission="*", pattern="C:\secret\*", decision="deny")
        self.director().call("POST", "/v1/director/presence", {})
        aid = self.ask(permission="edit", patterns=["C:/SECRET/payload"])
        self.assertEqual(self.approval(aid)["state"], "REJECTED")

    def test_m1_answers_follow_the_question(self):
        q = self.fake.add_question("ses_a1", options=("A", "B"))
        self.cycle()
        with self.assertRaises(Exception):  # an empty answer
            self.c.call("POST", "/v1/questions/answer", {"id": q["id"], "answers": [[]]})
        with self.h.d.store.tx() as conn:  # a question that allows only its own options
            conn.execute("UPDATE questions SET shape=? WHERE id=?",
                         (json.dumps([{"header": "h", "multiple": False, "custom": False, "options": ["A", "B"]}]),
                          q["id"]))
        with self.assertRaises(Exception):
            self.c.call("POST", "/v1/questions/answer", {"id": q["id"], "answers": [["maybe"]]})
        self.c.call("POST", "/v1/questions/answer", {"id": q["id"], "answers": [["A"]]})


class TestConnections(unittest.TestCase):
    def test_m5_an_idle_connection_is_closed_and_the_count_is_bounded(self):
        import socket
        from helpers import TempHome
        from imperium import daemon
        h = TempHome("[opencode]\npoll_interval = 3600.0\n").init().start()
        self.addCleanup(h.cleanup)
        old = daemon._Handler.timeout
        daemon._Handler.timeout = 1
        self.addCleanup(setattr, daemon._Handler, "timeout", old)
        s = socket.create_connection(("127.0.0.1", h.d.port))
        s.sendall(b"POST /v1/status HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n")  # never finishes
        s.settimeout(10)
        started = time.monotonic()
        try:
            data = s.recv(100)
        except OSError:
            data = b""
        self.assertLess(time.monotonic() - started, 9)  # the server gave up on it
        s.close()
        self.assertTrue(h.d.server.slots.acquire(blocking=False))  # its slot came back
        h.d.server.slots.release()
