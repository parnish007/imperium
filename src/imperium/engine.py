"""The polling engine: reads every builder, journals what it sees, and delivers its outbox (DESIGN §9.2, §10;
SYSTEM-DESIGN §2-§4).

Each batch from the reader commits its events and the builder's checkpoint in one transaction [R-2].
Internal retries are bounded: an unreachable server is retried with exponential backoff (at most 60 s
between attempts), never faster, never given up on; it is reported once after `unreachable_after` failures.

Delivery runs only right after a successful poll of the same builder, so eligibility is never decided on
stale state. A message is POSTed at most once: the DISPATCHING row is committed before the request, and
anything short of a definite answer is reconciled by looking for the message's own id, never by posting again.
"""
import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from . import acp, approvals, builders, journal, liveness, opencode, outbox, rounds, snapshot
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
        self.acp = {}  # builder name -> acp.Connection (agent processes Imperium owns)
        self.thread = None
        self.executor = ThreadPoolExecutor(max_workers=self.cfg['max_workers'], thread_name_prefix='imperium-poll')
        self.pending = {}  # builder -> future; a builder never has overlapping polls

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="imperium-engine", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(30)
        self.executor.shutdown(wait=True, cancel_futures=True)
        for conn in list(self.acp.values()):
            conn.close()
        self.acp.clear()

    def _loop(self):
        while not self.stop_event.wait(self.cfg["poll_interval"]):
            try:
                self.run_once(respect_backoff=True, wait_for_polls=False)
            except Exception:  # the loop must survive anything; the error is logged
                log.exception("engine cycle failed")

    def reset_backoff(self):
        for h in list(self.health.values()):
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

    def run_once(self, respect_backoff=False, wait_for_polls=True):
        with self.lock:
            if self.stop_event.is_set() or self.d.store.failed:
                return
            if time.monotonic() - self.verified_at >= self.d.cfg["integrity"]["verify_interval"]:
                self.verified_at = time.monotonic()
                c = self.d.refresh_chain()
                if not c["ok"]:
                    self.d.enter_quarantine(c)
            with self.d.store.read() as conn:
                rows = builders.list_(conn)
            secrets = self.secrets(rows)
            for name, future in list(self.pending.items()):
                if future.done():
                    del self.pending[name]
                    try:
                        future.result()
                    except Exception:
                        log.exception('builder poll failed: %s', name)
            for name in set(self.acp) - {b["name"] for b in rows if b["adapter"] == "acp"}:
                if name not in self.pending:
                    self.acp.pop(name).close()  # don't close an adapter while its worker is using it
            for b in rows:
                if self.stop_event.is_set():
                    return
                if b['name'] not in self.pending:
                    self.pending[b['name']] = self.executor.submit(self._poll, b, secrets, respect_backoff)
            current = list(self.pending.values())
        if wait_for_polls:  # deterministic entry point for tests and maintenance; production never waits here
            wait(current)
            for future in current:
                future.result()

    def state(self, name):
        h = self.health.get(name) or {}
        op = "UNREACHABLE" if h.get("reachable") is False else h.get("op_state")
        return {"reachable": h.get("reachable"), "failures": h.get("failures", 0), "last_error": h.get("last_error"),
                "dispatch_blocked": h.get("blocked"), "operational_state": op}

    def _poll(self, b, secrets, respect_backoff):
        name = b["name"]
        h = self.health.setdefault(name, {"failures": 0, "reachable": None, "next": 0.0, "auth_failed": False,
                                          "last_error": None})
        with self.d.store.read() as conn:
            cancelling = conn.execute("SELECT 1 FROM cancellations WHERE builder=? AND state IN "
                                      "('pending','sending','requested','acknowledged')", (name,)).fetchone()
        if not cancelling and (h.get("halted") or (respect_backoff and time.monotonic() < h["next"])):
            return
        if b["adapter"] == "acp":
            conn = self.acp.get(name)
            if conn is None:
                conn = self.acp[name] = acp.Connection(b, self.d.home, secrets)
            client = reader = conn
        else:
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
        self._abort(b, client)
        if h.get('halted'):
            return  # cancellation can still act when reading was halted by conflicting evidence
        with self.d.store.read() as conn:
            paused = bool(meta_get(conn, 'stop_all'))
        if b['adapter'] == 'acp' and paused and not client.alive:
            return  # a pause/abort must not start a new managed process
        if self.d.quarantine():
            return  # cancellation is allowed in quarantine; normal adapter work is not
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
    def _abort_status(self, b, request_id, state, detail):
        with self.d.store.tx() as conn:
            row = conn.execute('SELECT state FROM cancellations WHERE builder=? AND request_id=?',
                               (b['name'], request_id)).fetchone()
            if row is None or row[0] == state:
                return
            conn.execute('UPDATE cancellations SET state=?,detail=?,updated=? WHERE builder=? AND request_id=?',
                         (state, detail, self.clock(), b['name'], request_id))
            journal.append(conn, 'BUILDER_ABORT_STATUS', 'ACTION', builder=b['name'],
                           data={'request_id': request_id, 'state': state, 'detail': detail})

    def _abort(self, b, client):
        with self.d.store.read() as conn:
            row = conn.execute('SELECT * FROM cancellations WHERE builder=?', (b['name'],)).fetchone()
        if row is None or row['state'] not in ('pending', 'sending', 'requested', 'acknowledged'):
            return
        rid = row['request_id']
        if row['state'] == 'pending':
            self._abort_status(b, rid, 'sending', 'cancellation attempt recorded before contacting agent')
            try:
                if b['adapter'] == 'acp':
                    if not client.alive:
                        self._abort_status(b, rid, 'process_exited', 'managed process is not running; descendants unknown')
                        return
                    state = client.cancel()
                else:
                    client.abort(b['session_id'])
                    state = 'acknowledged'
                self._abort_status(b, rid, state, 'request sent; active-turn cessation is observed separately')
            except (opencode.OCError, opencode.OCUnreachable, OSError):
                self._abort_status(b, rid, 'uncertain', 'agent could not confirm cancellation; inspect it directly')
            return
        try:
            if b['adapter'] == 'acp':
                if client.cancel_confirmed:
                    self._abort_status(b, rid, 'confirmed', 'ACP active prompt answered with stopReason=cancelled')
                    return
                if not client.alive:
                    self._abort_status(b, rid, 'process_exited', 'managed process exited; descendants unknown')
                    return
                with client.lock:
                    idle = client.current is None
            else:
                idle = (client.status_map().get(b['session_id']) or {}).get('type', 'idle') == 'idle'
            if idle:
                self._abort_status(b, rid, 'idle_observed', 'session is idle; detached processes are not covered')
                return
        except (opencode.OCError, opencode.OCUnreachable):
            pass
        if self.clock() - row['updated'] >= 30:
            self._abort_status(b, rid, 'uncertain', 'cancellation not confirmed within 30 seconds')

    def _deliver(self, b, h, client):
        name = b["name"]
        with self.d.store.read() as conn:
            r = conn.execute("SELECT data FROM checkpoints WHERE builder=?", (name,)).fetchone()
            b = builders.get(conn, name)  # paused / allowed_version may have changed since the cycle began
            stop_all, observe = meta_get(conn, "stop_all"), meta_get(conn, "observe_only")
        cp = json.loads(r[0]) if r else {}
        idle = cp.get("status") == "idle" and not cp.get("open") and not cp.get("busy_children")
        h["idle_polls"] = h.get("idle_polls", 0) + 1 if idle else 0
        h["op_state"] = liveness.operational_state(cp, h, b, stop_all)
        lc = self.d.cfg["liveness"]
        alarm = liveness.check_stall(h, h["op_state"], cp, time.monotonic(), lc["stall_after"], lc["max_suppress"])
        if alarm:
            self._event(b, alarm[0], "ACTION", alarm[1])
        self._reconcile(b, cp, client)
        self._claim_files(b)
        self._replies(b, client)
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
        if b["adapter"] == "acp":
            if cp.get("protocol") != acp.PROTOCOL_VERSION:
                return f"ACP protocol {cp.get('protocol')} is not supported"
        elif v not in opencode.TESTED_VERSIONS and v != b["allowed_version"]:
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
        if cp.get("busy_children") is None and b['adapter'] != 'acp':
            return "sub-agents not yet checked"
        if cp["busy_children"]:
            return "a sub-agent is busy"
        if h.get("idle_polls", 0) < self.dcfg["idle_stable_polls"]:
            return "waiting for the builder to stay idle"
        with self.d.store.read() as conn:
            holder = outbox.holder(conn, b["name"])
            nxt = outbox.next_queued(conn, b["name"], owner_only=bool(b["paused"]))
        if holder:
            return f"message {holder} in flight"
        if nxt and nxt["needs_resources"]:
            verdict, why, _ = liveness.gate(self.d.cfg["resources"]["min_free_gb"])
            if verdict != "OK":
                return f"waiting for resources: {why}"
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

    def _replies(self, b, client):
        """Deliver decided approvals and answered questions to OpenCode; retried every cycle until delivered."""
        with self.d.store.read() as conn:
            due = approvals.replies_due(conn, b["name"])
            qdue = approvals.question_replies_due(conn, b["name"])
            braked = bool(meta_get(conn, "stop_all") or meta_get(conn, "quarantine"))
        if braked:
            # Under the brake only replies that stop work go out; a decided "once" or an answer waits for
            # resume-all (S5 review C5).
            due = [a for a in due if a["reply"] == "reject"]
            qdue = [q for q in qdue if q["state"] == approvals.REJECTED]
        for a in due:
            try:
                client.reply_permission(a["id"], a["reply"])
                ok = True
            except opencode.OCError as e:
                ok = e.status == 404  # already gone: nothing left to answer
            except opencode.OCUnreachable:
                continue
            with self.d.store.tx() as conn:
                if ok:
                    approvals.reply_sent(conn, a["id"], True)
        for q in qdue:
            try:
                if q["state"] == approvals.REJECTED:
                    client.reject_question(q["id"])
                else:
                    client.reply_question(q["id"], q["answers"])
                ok = True
            except opencode.OCError as e:
                ok = e.status == 404
            except opencode.OCUnreachable:
                continue
            if ok:
                with self.d.store.tx() as conn:
                    approvals.question_reply_sent(conn, q["id"])

    def _claim_files(self, b):
        """The fallback claim channel: `.imperium/claims/<round>.json` in the builder's workspace."""
        with self.d.store.read() as conn:
            live = conn.execute("SELECT id, claim_file_hash FROM rounds WHERE builder=? AND state IN "
                                f"({','.join('?' * len(rounds.LIVE))})", (b["name"], *rounds.LIVE)).fetchall()
        for rid, seen in live:
            path = rounds.claim_path(b["directory"], rid)
            try:
                with open(path, "rb") as f:
                    raw = f.read(256_000)
            except OSError:
                continue
            digest = hashlib.sha256(raw).hexdigest()
            if digest == seen:
                continue
            with self.d.store.tx() as conn:
                conn.execute("UPDATE rounds SET claim_file_hash=? WHERE id=?", (digest, rid))
                rounds.apply_claim_file(conn, builder=b["name"], rid=rid, raw=raw.decode("utf-8", "replace"),
                                        now=self.clock())

    def _base_snapshot(self, b):
        """Just before a round's opening message is sent: the code as the builder starts from it."""
        with self.d.store.read() as conn:
            m = outbox.next_queued(conn, b["name"], owner_only=bool(b["paused"]))
            if m is None or m["kind"] != "open" or not m["round"]:
                return
            r = rounds.get(conn, m["round"])
        if r["base_commit"]:
            return
        try:
            snap = snapshot.take(b["directory"], f"refs/imperium/{b['name']}/{r['id']}/base")
            snapshot.ensure_excluded(snap["top"])
        except (snapshot.SnapshotError, OSError) as e:
            self._event(b, "SNAPSHOT_FAILED", "NOTICE", {"round": r["id"], "kind": "base", "error": str(e)[:300],
                                                         "effect": "this round cannot be verified"})
            return
        with self.d.store.tx() as conn:
            conn.execute("UPDATE rounds SET base_commit=?, base_tree=? WHERE id=?", (snap["commit"], snap["tree"],
                                                                                    r["id"]))
            journal.append(conn, "SNAPSHOT_TAKEN", "INFO", builder=b["name"],
                           data={"round": r["id"], "generation": 1, "kind": "base", "tree": snap["tree"],
                                 "commit": snap["commit"], "ref": snap["ref"], "files": snap["files"]})

    def _dispatch(self, b, client):
        name, now = b["name"], self.clock()
        self._base_snapshot(b)
        with self.d.store.tx() as conn:
            if outbox.holder(conn, name):
                return
            # decided on this transaction, not on the read that began the cycle (S5 review C5)
            if meta_get(conn, "stop_all") or meta_get(conn, "observe_only") or meta_get(conn, "quarantine"):
                return
            m = outbox.next_queued(conn, name, owner_only=bool(b["paused"]))
            if m is None:
                return
            if m.get("round"):
                rr = conn.execute("SELECT state FROM rounds WHERE id=?", (m["round"],)).fetchone()
                if rr is not None and rr[0] in rounds.TERMINAL:  # never send into a decided round (S5 review C14)
                    outbox.transition(conn, m["id"], outbox.CANCELLED, event="MSG_CANCELLED",
                                      data={"note": f"round {m['round']} is {rr[0]}"})
                    return
            oc_id = opencode.message_id()
            extra, gen = rounds.header_fields(conn, m)
            body = rounds.body_for(conn, m, gen)
            outbox.reserve(conn, name, m["id"], now)
            outbox.transition(conn, m["id"], outbox.DISPATCHING, event="MSG_DISPATCHING",
                              data={"oc_message_id": oc_id}, oc_message_id=oc_id, dispatched_at=now, round_gen=gen)
        text = f"[imperium msg={m['id']} builder={name}{extra}]\n{body}"
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
                elif o["type"] == "PERMISSION_ASKED" and o.get("raw"):
                    if approvals.observe_ask(conn, b["name"], o["raw"], self.clock()):
                        lease_ok, director = self.d.lease_ok()
                        approvals.apply_policy(conn, o["raw"]["id"], b["directory"], lease_ok, director, self.clock())
                elif o["type"] == "PERMISSION_GONE":
                    approvals.gone(conn, o["data"]["permission_id"])
                elif o["type"] == "QUESTION_ASKED" and o.get("raw"):
                    approvals.observe_question(conn, b["name"], o["raw"], self.clock())
                elif o["type"] == "QUESTION_GONE":
                    approvals.question_gone(conn, o["data"]["question_id"])
                elif o["type"].startswith("ACP_"):
                    self._acp_observed(conn, b, o)
            conn.execute("INSERT INTO checkpoints(builder, data, updated) VALUES(?,?,?) "
                         "ON CONFLICT(builder) DO UPDATE SET data=excluded.data, updated=excluded.updated",
                         (b["name"], json.dumps(cp), journal.now()))
            if cp.get("version") and cp.get("version") != b.get("opencode_version"):
                conn.execute("UPDATE builders SET opencode_version=? WHERE name=?", (cp["version"], b["name"]))
                b["opencode_version"] = cp["version"]

    def _acp_observed(self, conn, b, o):
        d = o["data"]
        if o["type"] == "ACP_SESSION_CREATED":
            conn.execute("UPDATE builders SET session_id=? WHERE name=?", (d["session_id"], b["name"]))
            b["session_id"] = d["session_id"]
        elif o["type"] == "ACP_PROMPT_REFUSED":
            m = outbox.by_oc_id(conn, b["name"], d["message_id"])
            if m is not None and m["state"] == outbox.POSTED:
                outbox.transition(conn, m["id"], outbox.REJECTED, event="MSG_REJECTED", severity="ACTION",
                                  data={"note": "the agent answered with an error before any activity",
                                        "code": d.get("code")})
        elif o["type"] in ("ACP_HISTORY_TOKEN", "ACP_HISTORY_RAN"):
            # a replayed history (session/load) holds our header: proof, resolved to the message's own id
            try:
                m = outbox.get(conn, d["token_msg"])
            except outbox.OutboxError:
                return
            if m["builder"] != b["name"] or not m["oc_message_id"]:
                return
            if o["type"] == "ACP_HISTORY_TOKEN":
                outbox.observed_user(conn, b["name"], m["oc_message_id"], m["id"], self.clock())
                conn_ = self.acp.get(b["name"])
                if conn_ is not None:
                    conn_.seen.add(m["oc_message_id"])
            else:
                outbox.observed_turn(conn, b["name"], m["oc_message_id"], self.clock())

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
