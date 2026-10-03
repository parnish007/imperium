"""Verification jobs: snapshot the candidate, check the checks' own files, run each trusted check in a fresh
worktree of the snapshot, record provenance (DESIGN §7.4; SYSTEM-DESIGN §5; S4 review D3, D4, D6).

One job runs at a time, in its own thread; a check can take up to its timeout. Checks never run in the live
workspace and never through a shell. The environment is a minimal base plus the check's allow-list; names and
value hashes are recorded, values are not. The executable is resolved on that PATH and its hash recorded.
"""
import hashlib
import json
import logging
import os
import queue
import shutil
import signal
import subprocess
import threading
import time

from . import journal, outbox, rounds, snapshot, untrusted
from .store import StoreFailed, meta_get

log = logging.getLogger("imperiumd.verify")
BASE_ENV = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME", "USERPROFILE",
            "LANG", "LC_ALL", "TZ")
OUTPUT_CAP = 1_000_000


class VerifyError(RuntimeError):
    pass


def run_check(argv, cwd, env_names, timeout, out_path, secrets=(), *, cancel=None, environment=None):
    """Unsafe host runner, also used to supervise the Docker client (never candidate code in Docker mode).

    Output is drained with a hard byte limit. Cancellation, timeout and output exhaustion are execution errors,
    never evidence that a baseline assertion failed. Callers must explicitly choose this backend.
    """
    env = (dict(environment) if environment is not None else
           {k: v for k, v in os.environ.items() if k.upper() in BASE_ENV or k in env_names})
    if os.path.dirname(argv[0]):
        exe = argv[0] if os.path.isabs(argv[0]) else os.path.normpath(os.path.join(cwd, argv[0]))
    else:
        exe = shutil.which(argv[0], path=env.get("PATH") or env.get("Path")) or argv[0]
    try:
        with open(exe, "rb") as f:
            exe_hash = hashlib.file_digest(f, "sha256").hexdigest()
    except OSError:
        exe_hash = None
    kw = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else
          {"start_new_session": True})
    started = time.monotonic()
    error, timed_out, code = None, False, None
    output = bytearray()
    limit = threading.Event()
    cancel = cancel or threading.Event()
    p = None
    if cancel.is_set():
        error = "cancelled"
    else:
        try:
            p = subprocess.Popen([exe] + list(argv[1:]), cwd=cwd, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kw)
        except OSError as e:
            error = "start_failed"
            output.extend(str(e).encode()[:OUTPUT_CAP])
    if p is not None:
        def drain():
            try:
                while True:
                    chunk = p.stdout.read(8192)
                    if not chunk:
                        break
                    remaining = OUTPUT_CAP - len(output)
                    output.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        limit.set()
                        break
            except (OSError, ValueError):
                pass

        reader = threading.Thread(target=drain, daemon=True, name="imperium-check-output")
        reader.start()
        job = _Job.attach(p)
        try:
            while p.poll() is None:
                if cancel.is_set() or limit.is_set() or time.monotonic() - started >= timeout:
                    error = "cancelled" if cancel.is_set() else "output_limit" if limit.is_set() else "timeout"
                    timed_out = error == "timeout"
                    _kill_tree(p)
                    break
                cancel.wait(0.02)
            if job:
                job.close()
            elif os.name != "nt":
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                error = "cleanup_failed"
            reader.join(2)
            if reader.is_alive():
                error = "output_pipe_open"
            else:
                p.stdout.close()
            if limit.is_set():
                error = error or "output_limit"
            if cancel.is_set():
                error = "cancelled"
            code = p.returncode
        finally:
            if job:
                job.close()
    data = bytes(output)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(untrusted.redact(data.decode("utf-8", "replace"), secrets))
        if error:
            f.write(f"\n[execution error: {error}]\n")
    return {"exit_code": code, "timed_out": timed_out, "duration": round(time.monotonic() - started, 3),
            "output_sha256": hashlib.sha256(data).hexdigest(), "output_path": out_path,
            "executable": exe, "executable_sha256": exe_hash,
            "env_names": sorted((k, hashlib.sha256(v.encode()).hexdigest()[:12]) for k, v in env.items()),
            "execution": {"backend": "unsafe-local", "error": error, "output_bytes": len(data)}}


class _Job:
    """Windows: a Job Object holding a check and everything it starts; closing it kills them all, also processes
    that detached from the check. (The check runs for a moment before it is assigned; a child started in that
    moment is outside the job.)"""

    def __init__(self, handle):
        self.h = handle

    @classmethod
    def attach(cls, p):
        if os.name != "nt":
            return None
        import ctypes
        from ctypes import wintypes as wt
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wt.HANDLE
        k32.CreateJobObjectW.argtypes = [wt.LPVOID, wt.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, wt.LPVOID, wt.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        k32.CloseHandle.argtypes = [wt.HANDLE]

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        h = k32.CreateJobObjectW(None, None)
        if not h:
            return None
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)) and \
            k32.AssignProcessToJobObject(h, int(p._handle))
        if not ok:
            k32.CloseHandle(h)
            log.warning("could not put check process %s in a job object (%s)", p.pid, ctypes.get_last_error())
            return None
        job = cls(h)
        job.k32 = k32
        return job

    def close(self):
        if self.h:
            self.k32.CloseHandle(self.h)
            self.h = None


def _kill_tree(p):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        p.kill()
    except OSError:
        pass


def file_hash(path):
    with open(path, "rb") as f:
        return "sha256:" + hashlib.file_digest(f, "sha256").hexdigest()


def hash_depends(directory, paths, repo_relative=False):
    """Record what each file a check depends on is now. Relative paths are inside the builder's repository
    (git blob ids); absolute paths are outside it (held-out checks; sha256). Relative paths are taken from the
    builder's directory, or from the repository's top with `repo_relative` (as recorded)."""
    out = {}
    top = prefix = None
    for p in paths:
        if os.path.isabs(p):
            if not os.path.isfile(p):
                raise VerifyError(f"{p} does not exist")
            out[os.path.normpath(p)] = file_hash(p)
            continue
        if top is None:
            top, prefix = snapshot.repo_root(directory)
        if '..' in p.replace('\\', '/').split('/'):
            raise VerifyError('relative check dependencies must stay inside the repository')
        rel = "/".join(x for x in ([] if repo_relative else [prefix]) + p.replace("\\", "/").split("/")
                       if x and x != ".")
        h = snapshot.hash_file(top, rel)
        if h is None:
            raise VerifyError(f"{p} does not exist in {directory}")
        out[rel] = h
    return out


class Verifier:
    def __init__(self, daemon):
        self.d = daemon
        self.q = queue.Queue()
        self.cond = threading.Condition()
        self.busy = 0
        self.thread = None
        self.stop_event = threading.Event()
        self.cancel_event = threading.Event()
        self.cancel_epoch = 0

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="imperium-verify", daemon=True)
        self.thread.start()

    def stop(self):
        self.abort_all()
        self.stop_event.set()
        self.q.put(None)
        if self.thread:
            # Container inspection/removal has bounded RPC timeouts. Do not close SQLite while this worker
            # is still recording the cancelled job or releasing its reservation.
            self.thread.join()

    def submit(self, rid, job):
        with self.cond:
            self.busy += 1
            self.q.put((rid, job, self.cancel_epoch))

    def abort_all(self):
        with self.cond:
            self.cancel_epoch += 1
            self.cancel_event.set()
        return {"state": "requested", "note": "active and queued checks are cancelled; watch VERIFY_FAILED"}

    def wait_idle(self, timeout=60):
        end = time.monotonic() + timeout
        with self.cond:
            while self.busy:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self.cond.wait(left)
        return True

    def _loop(self):
        while not self.stop_event.is_set():
            item = self.q.get()
            if item is None:
                return
            rid, job, epoch = item
            try:
                with self.cond:
                    cancelled = epoch != self.cancel_epoch
                    if not cancelled:
                        self.cancel_event = threading.Event()
                if cancelled:
                    self._finish(rid, job, error="verification cancelled before execution")
                else:
                    self.run(rid, job)
            except Exception:  # one failed job must not stop the worker
                log.exception("verification of %s failed", rid)
                self._finish(rid, job, error="internal error; see the daemon log")
            finally:
                with self.cond:
                    self.busy -= 1
                    self.cond.notify_all()

    def _finish(self, rid, job, error=None):
        try:
            with self.d.store.tx() as conn:
                r = rounds.get(conn, rid)
                if r["verify_job"] == job:
                    conn.execute("UPDATE rounds SET verify_job=NULL WHERE id=?", (rid,))
                if error:
                    journal.append(conn, "VERIFY_FAILED", "ACTION", builder=r["builder"],
                                   data={"round": rid, "generation": r["generation"], "error": error[:300]})
        except StoreFailed:
            pass

    def run(self, rid, job):
        clock = self.d.engine.clock
        with self.d.store.read() as conn:
            r = rounds.get(conn, rid)
            b = conn.execute("SELECT * FROM builders WHERE name=?", (r["builder"],)).fetchone()
            cp_row = conn.execute("SELECT data FROM checkpoints WHERE builder=?", (r["builder"],)).fetchone()
        cp = json.loads(cp_row[0]) if cp_row else {}
        if cp.get("status") != "idle" or cp.get("open"):
            return self._finish(rid, job, error="the builder is not idle; verify when it has stopped working")
        gen = r["generation"]
        holder = f"verify:{rid}"
        with self.d.store.tx() as conn:
            if outbox.holder(conn, r["builder"]):
                pass  # reported below, outside the transaction
            else:
                outbox.reserve(conn, r["builder"], holder, clock())
        with self.d.store.read() as conn:
            if outbox.holder(conn, r["builder"]) != holder:
                return self._finish(rid, job, error="a message is in flight to the builder; verify when it is done")
        try:
            snap = snapshot.take(b["directory"], f"refs/imperium/{r['builder']}/{rid}/candidate-g{gen}")
        except snapshot.SnapshotError as e:
            with self.d.store.tx() as conn:
                outbox.release(conn, r["builder"], holder)
            return self._finish(rid, job, error=f"snapshot failed: {e}")
        with self.d.store.tx() as conn:
            outbox.release(conn, r["builder"], holder)
            rounds.void_verification(conn, rid, clock(), "a new candidate snapshot was taken")
            conn.execute("UPDATE rounds SET cand_commit=?, cand_tree=?, cand_generation=?, untrusted=0, "
                         "checks_ok_generation=NULL, updated=? WHERE id=?",
                         (snap["commit"], snap["tree"], gen, clock(), rid))
            journal.append(conn, "SNAPSHOT_TAKEN", "INFO", builder=r["builder"],
                           data={"round": rid, "generation": gen, "kind": "candidate", "tree": snap["tree"],
                                 "commit": snap["commit"], "ref": snap["ref"], "files": snap["files"]})
            checks = rounds.checks_for(conn, r["builder"], rid)
        top, prefix = snap["top"], snap["prefix"]
        events = []
        # 1. the checks' own files: changed by the builder = untrusted [D5]
        untrusted_checks = []
        for c in checks:
            for path, want in c["depends"].items():
                if os.path.isabs(path):
                    have = file_hash(path) if os.path.isfile(path) else None
                else:
                    have = snapshot.blob(top, snap["commit"], path)
                if have != want:
                    untrusted_checks.append({"check": c["id"], "path": path, "missing": have is None})
        # Refuse before *any* check executes, including optional checks. A later rejection cannot undo effects.
        if untrusted_checks:
            with self.d.store.tx() as conn:
                conn.execute("UPDATE rounds SET untrusted=1 WHERE id=?", (rid,))
                journal.append(conn, "UNTRUSTED_CHECKS", "ACTION", builder=r["builder"],
                               data={"round": rid, "generation": gen, "changed": untrusted_checks,
                                     "note": "no checks executed; owner review and approval are required"})
                journal.append(conn, "TEST_FILES_CHANGED", "ACTION", builder=r["builder"],
                               data={"round": rid, "files": [x['path'] for x in untrusted_checks]})
            return self._finish(rid, job, error="trusted check dependencies changed; no checks executed")
        # 2. test files changed in the round (a flag, never a block)
        if r["base_commit"]:
            changed = [p for _, p in snapshot.changed_files(top, r["base_commit"], snap["commit"])]
            tests = [p for p in changed if rounds.is_test_path(p)]
            if tests:
                events.append(("TEST_FILES_CHANGED", "ACTION" if untrusted_checks else "NOTICE",
                               {"files": tests[:50], "count": len(tests)}))
        # 3. run the checks
        secrets = self.d.engine.secrets([dict(b)]) if self.d.engine else []
        out_dir = os.path.join(self.d.home, "checks")
        results, failed = [], []
        for c in checks:
            if self.cancel_event.is_set():
                failed.append("verification cancelled")
                break
            res = self._run_on(top, prefix, snap["commit"], c, out_dir, rid, gen, "candidate", secrets)
            ok = res["exit_code"] == 0 and not res["timed_out"] and not res.get('execution', {}).get('error')
            entry = {"check": c["id"], "version": c["version"], "passed": ok, "exit_code": res["exit_code"],
                     "timed_out": res["timed_out"], "required": c["required"]}
            if c["must_fail_on_base"]:
                if not r["base_commit"]:
                    entry["discriminating"] = None
                    if c["required"]:
                        failed.append(f"{c['id']}: no base snapshot to show it fails without the change")
                else:
                    base = self._run_on(top, prefix, r["base_commit"], c, out_dir, rid, gen, "base", secrets)
                    entry["discriminating"] = (base["exit_code"] in c['base_failure_codes'] and
                                               not base["timed_out"] and
                                               not base.get('execution', {}).get('error'))
                    if not entry["discriminating"]:
                        events.append(("GATE_NOT_DISCRIMINATING", "ACTION",
                                       {"check": c["id"], "expected_codes": c['base_failure_codes'],
                                        "exit_code": base['exit_code'], "execution": base.get('execution', {}),
                                        "note": "baseline did not produce the configured test-failure outcome"}))
                        if c["required"]:
                            failed.append(f"{c['id']}: baseline did not produce the expected test failure")
            if not ok and c["required"]:
                failed.append(f"{c['id']}: " + ("timed out" if res["timed_out"] else f"exit {res['exit_code']}"))
            results.append(entry)
        with self.d.store.tx() as conn:
            r2 = rounds.get(conn, rid)
            # nothing is published from a quarantined journal, and results only cover the checks that ran
            # (S5 review C15, C3)
            if meta_get(conn, "quarantine"):
                failed.append("Imperium is quarantined (journal integrity); results are not used")
            if self.cancel_event.is_set():
                failed.append("verification cancelled; results are not used")
            ran = sorted((c["id"], c["version"]) for c in checks)
            if ran != sorted((c["id"], c["version"]) for c in rounds.checks_for(conn, r["builder"], rid)):
                failed.append("the checks changed while they were running; verify again")
            for type_, sev, data in events:
                journal.append(conn, type_, sev, builder=r["builder"], data={"round": rid, "generation": gen, **data})
            if not checks:
                failed.append("no checks are defined for this round or builder")
            if failed:
                journal.append(conn, "VERIFY_FAILED", "ACTION", builder=r["builder"],
                               data={"round": rid, "generation": gen, "failed": failed, "results": results})
            else:
                conn.execute("UPDATE rounds SET checks_ok_generation=? WHERE id=? AND cand_generation=?",
                             (gen, rid, gen))
                journal.append(conn, "CHECKS_PASSED", "NOTICE", builder=r["builder"],
                               data={"round": rid, "generation": gen, "results": results,
                                     "tree": snap["tree"]})
                if r2["state"] == rounds.VERIFIED or r2["generation"] == gen:
                    rounds._maybe_verified(conn, rid, clock())
            if r2["verify_job"] == job:
                conn.execute("UPDATE rounds SET verify_job=NULL WHERE id=?", (rid,))

    def _run_on(self, top, prefix, commit, c, out_dir, rid, gen, target, secrets):
        from . import check_runner
        clock = self.d.engine.clock
        with snapshot.Worktree(top, commit) as wt:
            cwd = os.path.normpath(os.path.join(wt, *[x for x in (prefix + "/" + c["working_dir"]).split("/")
                                                      if x and x != "."]))
            if not os.path.isdir(cwd):
                res = check_runner.failure('working directory is not in the snapshot',
                                           self.d.cfg['verification']['backend'])
            else:
                name = f"{rid}-g{gen}-{c['id']}-{target}-{int(time.time() * 1000)}.log"
                res = check_runner.run(self.d.cfg['verification'], bool(self.d.cfg['isolation']['owner_accounts']),
                                       c['argv'], wt, cwd, set(c['env']), c['timeout'], os.path.join(out_dir, name),
                                       c['depends'], secrets, cancel=self.cancel_event)
        with self.d.store.tx() as conn:
            rounds.record_run(conn, rid=rid, generation=gen, check=c, target=target, commit=commit,
                              tree=snapshot._git(["rev-parse", f"{commit}^{{tree}}"], top), result=res, now=clock())
        return res
