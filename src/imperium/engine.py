"""The polling engine: reads every builder and journals what it sees (DESIGN §9.2, §10).

Each batch from the reader commits its events and the builder's checkpoint in one transaction [R-2].
Internal retries are bounded: an unreachable server is retried with exponential backoff (at most 60 s
between attempts), never faster, never given up on; it is reported once after `unreachable_after` failures.
"""
import json
import logging
import os
import threading
import time

from . import builders, journal, opencode
from .reader import Reader
from .store import StoreFailed

log = logging.getLogger("imperiumd.engine")


class Engine:
    def __init__(self, daemon):
        self.d = daemon
        self.cfg = daemon.cfg["opencode"]
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.health = {}
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="imperium-engine", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(30)

    def _loop(self):
        while not self.stop_event.wait(self.cfg["poll_interval"]):
            try:
                self.run_once(respect_backoff=True)
            except Exception:  # the loop must survive anything; the error is logged
                log.exception("engine cycle failed")

    def reset_backoff(self):
        for h in self.health.values():
            h["next"] = 0.0

    def secrets(self, rows):
        values = []
        for b in rows:
            try:
                values.append(builders.password(b))
            except OSError:
                pass
        values += [os.environ.get(n) for n in self.d.cfg["redaction"]["env_names"]]
        return [v for v in values if v]

    def run_once(self, respect_backoff=False):
        with self.lock:
            if self.d.store.failed or self.d.quarantine():
                return
            with self.d.store.read() as conn:
                rows = builders.list_(conn)
            secrets = self.secrets(rows)
            for b in rows:
                if self.stop_event.is_set():
                    return
                self._poll(b, secrets, respect_backoff)

    def state(self, name):
        h = self.health.get(name) or {}
        return {"reachable": h.get("reachable"), "failures": h.get("failures", 0), "last_error": h.get("last_error")}

    def _poll(self, b, secrets, respect_backoff):
        name = b["name"]
        h = self.health.setdefault(name, {"failures": 0, "reachable": None, "next": 0.0, "auth_failed": False,
                                          "last_error": None})
        if h.get("halted") or (respect_backoff and time.monotonic() < h["next"]):
            return
        try:
            pw = builders.password(b)
        except OSError as e:
            self._fail(b, h, "credential", f"cannot read the password file: {e.strerror}")
            return
        client = opencode.OpenCodeClient(b["endpoint"], directory=b["directory"], password=pw,
                                         timeout=self.cfg["timeout"])
        reader = Reader(client, b, secrets=secrets, page_size=self.cfg["page_size"],
                        max_scan_pages=self.cfg["max_scan_pages"], partless_polls=self.cfg["partless_polls"])
        with self.d.store.read() as conn:
            r = conn.execute("SELECT data FROM checkpoints WHERE builder=?", (name,)).fetchone()
        cp = json.loads(r[0]) if r else None
        try:
            for obs, new_cp in reader.poll(cp):
                self._commit(b, obs, new_cp)
        except opencode.OCUnreachable as e:
            self._fail(b, h, "unreachable", str(e))
            return
        except opencode.OCError as e:
            if e.status in (401, 403):
                if not h["auth_failed"]:
                    h["auth_failed"] = True
                    self._event(b, "BUILDER_AUTH_FAILED", "ACTION",
                                {"status": e.status, "credential": "env:" + b["password_env"] if b["password_env"]
                                 else ("file" if b["password_file"] else "none"),
                                 "hint": "set the builder's password variable before `imperium up`"})
                self._backoff(h)
                return
            self._fail(b, h, f"http {e.status}", str(e))
            return
        except journal.SourceKeyConflict as e:
            # An adapter produced two different observations under one key: stop reading this builder
            # rather than guess which one is true. Restarting the daemon retries.
            h["halted"] = True
            self._event(b, "SOURCE_KEY_CONFLICT", "CRITICAL", {"error": str(e)[:300],
                        "effect": "this builder is no longer read until the daemon restarts"})
            return
        except (KeyError, TypeError, ValueError, AttributeError) as e:  # a response of an unexpected shape [R-20]
            if h["last_error"] != f"malformed: {type(e).__name__}":
                h["last_error"] = f"malformed: {type(e).__name__}"
                self._event(b, "ADAPTER_ERROR", "NOTICE", {"error": type(e).__name__,
                                                           "note": "OpenCode returned an unexpected shape"})
            self._backoff(h)
            return
        except StoreFailed:
            return
        if h["reachable"] is False:
            self._event(b, "BUILDER_REACHABLE", "INFO", {"after_failures": h["failures"]})
        if h["auth_failed"]:
            self._event(b, "BUILDER_AUTH_OK", "INFO")
        h.update({"failures": 0, "reachable": True, "next": 0.0, "auth_failed": False, "last_error": None})

    def _commit(self, b, obs, cp):
        with self.d.store.tx() as conn:
            for o in obs:
                journal.append(conn, o["type"], o["severity"], builder=b["name"], data=o["data"],
                               untrusted=o["untrusted"], source_key=o["source_key"], caller="adapter")
            conn.execute("INSERT INTO checkpoints(builder, data, updated) VALUES(?,?,?) "
                         "ON CONFLICT(builder) DO UPDATE SET data=excluded.data, updated=excluded.updated",
                         (b["name"], json.dumps(cp), journal.now()))
            if cp.get("version") and cp.get("version") != b.get("opencode_version"):
                conn.execute("UPDATE builders SET opencode_version=? WHERE name=?", (cp["version"], b["name"]))
                b["opencode_version"] = cp["version"]

    def _event(self, b, type_, severity, data=None):
        try:
            with self.d.store.tx() as conn:
                journal.append(conn, type_, severity, builder=b["name"], data=data or {}, caller="adapter")
        except StoreFailed:
            pass

    def _backoff(self, h):
        h["failures"] += 1
        h["next"] = time.monotonic() + min(60.0, 2.0 ** min(h["failures"], 6))

    def _fail(self, b, h, kind, message):
        self._backoff(h)
        h["last_error"] = f"{kind}: {message}"[:300]
        if h["failures"] >= self.cfg["unreachable_after"] and h["reachable"] is not False:
            h["reachable"] = False
            self._event(b, "BUILDER_UNREACHABLE", "ACTION", {"failures": h["failures"], "error": kind})
