"""The polling engine: reads every builder, journals what it sees, and delivers its outbox (DESIGN §9.2, §10;
SYSTEM-DESIGN §2-§4).

Each batch from the reader commits its events and the builder's checkpoint in one transaction [R-2].
Internal retries are bounded: an unreachable server is retried with exponential backoff (at most 60 s
between attempts), never faster, never given up on; it is reported once after `unreachable_after` failures.

Delivery runs only right after a successful poll of the same builder, so eligibility is never decided on
stale state. A message is POSTed at most once: the DISPATCHING row is committed before the request, and
anything short of a definite answer is reconciled by looking for the message's own id, never by posting again.
"""
import json
import logging
import os
import threading
import time

from . import builders, journal, opencode, outbox
from .reader import Reader
from .store import StoreFailed, meta_get

log = logging.getLogger("imperiumd.engine")


class Engine:
    def __init__(self, daemon):
        self.d = daemon
        self.cfg = daemon.cfg["opencode"]
        self.dcfg = daemon.cfg["delivery"]
        self.clock = time.time  # replaced in tests to move time forward
        self.verified_at = time.monotonic()
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
            if time.monotonic() - self.verified_at >= self.d.cfg["integrity"]["verify_interval"]:
                self.verified_at = time.monotonic()
                c = self.d.refresh_chain()
                if not c["ok"]:
                    self.d.enter_quarantine(c)
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
        return {"reachable": h.get("reachable"), "failures": h.get("failures", 0), "last_error": h.get("last_error"),
                "dispatch_blocked": h.get("blocked")}

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
        try:
            self._deliver(b, h, client)
        except StoreFailed:
            return

    # --- delivery ---------------------------------------------------------------------------------------
    def _deliver(self, b, h, client):
        name = b["name"]
        with self.d.store.read() as conn:
            r = conn.execute("SELECT data FROM checkpoints WHERE builder=?", (name,)).fetchone()
            b = builders.get(conn, name)  # paused / allowed_version may have changed since the cycle began
            stop_all, observe = meta_get(conn, "stop_all"), meta_get(conn, "observe_only")
        cp = json.loads(r[0]) if r else {}
        idle = cp.get("status") == "idle" and not cp.get("open") and not cp.get("busy_children")
        h["idle_polls"] = h.get("idle_polls", 0) + 1 if idle else 0
        self._reconcile(b, cp, client)
        reason = self._blocked(b, cp, h, stop_all, observe)
        h["blocked"] = reason
        if reason is None:
            h.pop("stalled", None)
            self._dispatch(b, client)
        else:
            self._stall(b, h, reason)

    def _stall(self, b, h, reason):
        """Messages waiting on a builder that stays blocked are reported once per episode: unattended, a queue
        that silently never moves is a failure nobody sees."""
        with self.d.store.read() as conn:
            r = conn.execute("SELECT MIN(created), COUNT(*) FROM outbox WHERE builder=? AND state='QUEUED'",
                             (b["name"],)).fetchone()
        if not r[1]:
            h.pop("stalled", None)
            return
        waited = self.clock() - r[0]
        if waited >= self.dcfg["stall_alert"] and h.get("stalled") != reason:
            h["stalled"] = reason
            self._event(b, "DISPATCH_STALLED", "ACTION", {"reason": reason, "queued": r[1],
                                                          "oldest_waited_s": round(waited, 1)})

    def _blocked(self, b, cp, h, stop_all, observe):
        """Why nothing may be sent to this builder now, or None. Unknown counts as blocked."""
        if stop_all:
            return f"stop-all: {stop_all}"
        if observe:
            return "observe-only after a restore"
        if not cp.get("attached") or cp.get("session_missing") or cp.get("session_mismatch"):
            return "session not attached"
        if cp.get("phase") is not None:
            return "still reading history"
        v = cp.get("version")
        if v not in opencode.TESTED_VERSIONS and v != b["allowed_version"]:
            return f"OpenCode {v} is untested; the owner may run `imperium builder allow-version`"
        if cp.get("permissions") is None:
            return "permission state unknown"
        if cp["permissions"]:
            return "permission pending"
        if cp.get("questions"):
            return "question pending"
        if cp.get("status") != "idle":
            return f"builder {cp.get('status')}"
        if cp.get("open"):
            return "a message is still being written"
        if cp.get("busy_children") is None:
            return "sub-agents not yet checked"
        if cp["busy_children"]:
            return "a sub-agent is busy"
        if h.get("idle_polls", 0) < self.dcfg["idle_stable_polls"]:
            return "waiting for the builder to stay idle"
        with self.d.store.read() as conn:
            holder = outbox.holder(conn, b["name"])
        if holder:
            return f"message {holder} in flight"
        return None

    def _reconcile(self, b, cp, client):
        """Settle the builder's in-flight message from what can be observed; never by sending it again."""
        now = self.clock()
        with self.d.store.read() as conn:
            m = outbox.in_flight(conn, b["name"])
        if m is None or m["state"] == outbox.DISPATCHING:
            return
        if m["state"] in (outbox.POSTED, outbox.UNKNOWN, outbox.UNCERTAIN):
            try:
                client.message(b["session_id"], m["oc_message_id"])
                found = True
            except opencode.OCError as e:
                if e.status != 404:
                    return
                found = False
            except opencode.OCUnreachable:
                return
            with self.d.store.tx() as conn:
                m = outbox.get(conn, m["id"])
                if found and m["state"] in (outbox.POSTED, outbox.UNKNOWN):
                    outbox.transition(conn, m["id"], outbox.DELIVERED, event="MSG_DELIVERED", delivered_at=now,
                                      data={"proof": "message id found by direct lookup"})
                elif found and m["state"] == outbox.UNCERTAIN:
                    outbox.transition(conn, m["id"], outbox.DELIVERED, event="MSG_FOUND_LATE", severity="NOTICE",
                                      delivered_at=now, data={"proof": "message id found by direct lookup"})
                elif (not found and m["state"] in (outbox.POSTED, outbox.UNKNOWN)
                      and now - m["dispatched_at"] > self.dcfg["reconcile_window"]):
                    outbox.transition(conn, m["id"], outbox.UNCERTAIN, event="MSG_UNCERTAIN", severity="ACTION",
                                      data={"waited_s": round(now - m["dispatched_at"], 1),
                                            "note": "not found in the builder; it may or may not run. Decide: "
                                                    "wait, cancel, or resend (may run twice)"})
            return
        if m["state"] == outbox.DELIVERED and now - m["delivered_at"] > self.dcfg["admit_timeout"]:
            quiet = (cp.get("status") == "idle" and not cp.get("open") and not cp.get("busy_children")
                     and not cp.get("permissions") and not cp.get("questions"))
            if quiet:
                with self.d.store.tx() as conn:
                    if outbox.get(conn, m["id"])["state"] == outbox.DELIVERED:
                        outbox.transition(conn, m["id"], outbox.STRANDED, event="MSG_STRANDED", severity="ACTION",
                                          data={"waited_s": round(now - m["delivered_at"], 1),
                                                "note": "saved in the builder but not run while it is idle; "
                                                        "nothing else is sent to it until a decision"})

    def _dispatch(self, b, client):
        name, now = b["name"], self.clock()
        with self.d.store.tx() as conn:
            if outbox.holder(conn, name):
                return
            m = outbox.next_queued(conn, name, owner_only=bool(b["paused"]))
            if m is None:
                return
            oc_id = opencode.message_id()
            outbox.reserve(conn, name, m["id"], now)
            outbox.transition(conn, m["id"], outbox.DISPATCHING, event="MSG_DISPATCHING",
                              data={"oc_message_id": oc_id}, oc_message_id=oc_id, dispatched_at=now)
        text = f"[imperium msg={m['id']} builder={name}]\n{m['body']}"
        try:
            client.prompt_async(b["session_id"], oc_id, [{"type": "text", "text": text}])
            to, event, sev, data = outbox.POSTED, "MSG_POSTED", "INFO", {}
        except opencode.OCError as e:
            if e.status in (401, 403, 429):
                with self.d.store.tx() as conn:
                    outbox.requeue(conn, m["id"], f"http {e.status}")
                return
            if 400 <= e.status < 500:
                to, event, sev, data = outbox.REJECTED, "MSG_REJECTED", "ACTION", {"status": e.status}
            else:
                to, event, sev, data = outbox.UNKNOWN, "MSG_UNKNOWN", "NOTICE", {"status": e.status}
        except Exception as e:  # transport failure or anything unexpected: it may have been saved
            to, event, sev, data = outbox.UNKNOWN, "MSG_UNKNOWN", "NOTICE", {"error": type(e).__name__}
        with self.d.store.tx() as conn:
            outbox.transition(conn, m["id"], to, event=event, severity=sev, data=data)

    def _commit(self, b, obs, cp):
        with self.d.store.tx() as conn:
            for o in obs:
                journal.append(conn, o["type"], o["severity"], builder=b["name"], data=o["data"],
                               untrusted=o["untrusted"], source_key=o["source_key"], caller="adapter")
                if o["type"] == "USER_MESSAGE_TOKEN":
                    outbox.observed_user(conn, b["name"], o["data"]["message_id"], o["data"]["token_msg"],
                                         self.clock())
                elif o["type"] == "TURN_STARTED" and o["data"].get("parent_id"):
                    outbox.observed_turn(conn, b["name"], o["data"]["parent_id"], self.clock())
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
