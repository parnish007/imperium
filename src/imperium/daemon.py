"""imperiumd: the single writer. Local HTTP API on 127.0.0.1 with bearer tokens (DESIGN §3, §11.2, §11.3)."""
import argparse
import datetime
import http.server
import json
import logging
import logging.handlers
import os
import secrets
import socketserver
import sqlite3
import sys
import threading
import time
import urllib.parse

from . import __version__, audit, backup, builders, config, feeds, journal, opencode, paths, retention, tokens
from .engine import Engine
from . import fsutil
from .store import Store, StoreFailed, meta_get, meta_set

log = logging.getLogger("imperiumd")


class AlreadyRunning(RuntimeError):
    pass


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# --- single-instance lock -------------------------------------------------------------------------

def acquire_lock(home):
    """Hold an OS lock on `imperium.lock`; the OS releases it if the process dies."""
    f = open(paths.lockfile(home), "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise AlreadyRunning("another Imperium process holds the lock (is the daemon running?)") from None
    return f


def release_lock(f):
    if f is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    f.close()


# --- rate limiting ----------------------------------------------------------------------------------

class Buckets:
    """Token bucket per principal [P2-13]."""

    def __init__(self, rate, burst):
        self.rate, self.burst = rate, burst
        self.state = {}
        self.lock = threading.Lock()

    def allow(self, key):
        now = time.monotonic()
        with self.lock:
            tokens_left, last = self.state.get(key, (float(self.burst), now))
            tokens_left = min(float(self.burst), tokens_left + (now - last) * self.rate)
            if tokens_left < 1.0:
                self.state[key] = (tokens_left, now)
                return False
            self.state[key] = (tokens_left - 1.0, now)
            return True


# --- routes -----------------------------------------------------------------------------------------
# Each route: (handler, who may call it: None | "any" | "owner", writes state).

def r_health(d, principal, body, query):
    return {"run_id": d.run_id, "version": __version__, "pid": d.pid}


def r_status(d, principal, body, query):
    with d.store.read() as conn:
        head_seq, _ = journal.head(conn)
        consumers = []
        needs_you = 0
        for c in conn.execute("SELECT * FROM consumers ORDER BY name"):
            sev = max(c["floor"], journal.RANK["ACTION"])
            unacked = conn.execute("SELECT COUNT(*) FROM events WHERE seq > ? AND severity >= ?",
                                   (c["acked_seq"], sev)).fetchone()[0]
            mine = c["principal"] == principal
            if mine:
                needs_you += unacked
            consumers.append({"name": c["name"], "principal": c["principal"], "floor": journal.SEVERITIES[c["floor"]],
                              "acked_seq": c["acked_seq"], "shown_through": c["shown_through"],
                              "unacked_action_or_critical": unacked})
        observe_only = meta_get(conn, "observe_only")
        dropped = audit.dropped(conn)
        bl = []
        for b in builders.list_(conn):
            r = conn.execute("SELECT data FROM checkpoints WHERE builder=?", (b["name"],)).fetchone()
            cp = json.loads(r[0]) if r else {}
            bl.append({"name": b["name"], "endpoint": b["endpoint"], "session_id": b["session_id"],
                       "opencode_version": b["opencode_version"], "status": cp.get("status"),
                       "permissions_pending": cp.get("permissions"), "questions_pending": cp.get("questions"),
                       **(d.engine.state(b["name"]) if d.engine else {})})
    return {"run_id": d.run_id, "pid": d.pid, "port": d.port, "started": d.started, "version": __version__,
            "principal": principal, "head_seq": head_seq, "chain": d.chain, "archives": d.archives,
            "observe_only": observe_only, "quarantine": d.quarantine(), "consumers": consumers,
            "needs_you": needs_you, "audit_dropped": dropped, "builders": bl}


def r_builders(d, principal, body, query):
    name = query.get("name", [None])[0]
    with d.store.read() as conn:
        if name:
            return {"builder": builders.get(conn, name)}
        return {"builders": builders.list_(conn)}


def r_builder_add(d, principal, body, query):
    name, directory = str(_req(body, "name")), str(_req(body, "directory"))
    session_id = str(_req(body, "session_id"))
    pw_env, pw_file = body.get("password_env"), body.get("password_file")
    if pw_env and pw_file:
        raise HttpError(400, "give either a password variable or a password file, not both")
    endpoint = builders.validate(name, str(_req(body, "endpoint")))
    version = None
    if body.get("check", True):
        try:
            pw = builders.password({"password_env": pw_env, "password_file": pw_file})
        except OSError as e:
            raise HttpError(409, f"cannot read the password file: {e.strerror}") from None
        client = opencode.OpenCodeClient(endpoint, directory=directory, password=pw,
                                         timeout=d.cfg["opencode"]["timeout"])
        try:
            version = (client.health() or {}).get("version")
            sess = client.session(session_id)
        except opencode.OCUnreachable:
            raise HttpError(409, f"cannot reach {endpoint}; is `opencode serve` running there?") from None
        except opencode.OCError as e:
            if e.status == 404:
                raise HttpError(409, f"session {session_id} not found on {endpoint} in {directory}") from None
            if e.status in (401, 403):
                raise HttpError(409, "the OpenCode server refused the password") from None
            raise HttpError(409, str(e)) from None
        if builders.norm_dir(sess.get("directory") or "") != builders.norm_dir(directory):
            raise HttpError(409, f"session {session_id} belongs to {sess.get('directory')}, not {directory}")
    with d.store.tx() as conn:
        b = builders.add(conn, name=name, endpoint=endpoint, session_id=session_id, directory=directory,
                         password_env=pw_env, password_file=pw_file, version=version, caller=principal)
    return {"builder": b}


def r_builder_remove(d, principal, body, query):
    with d.store.tx() as conn:
        builders.remove(conn, str(_req(body, "name")), caller=principal)
    return {"removed": body["name"]}


def _active_director(conn):
    return conn.execute("SELECT * FROM directors WHERE released_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()


def r_director_show(d, principal, body, query):
    with d.store.read() as conn:
        r = _active_director(conn)
    return {"director": None if r is None else {"session_id": r["session_id"], "claimed_at": r["claimed_at"],
                                                "claimed_by": r["claimed_by"]}}


def r_director_claim(d, principal, body, query):
    """The owner registers a Claude Code session as the director (DESIGN §8.4) [C9]."""
    sid = str(_req(body, "session_id"))
    who = "director:" + sid
    tokens.check_principal(who)
    with d.store.tx() as conn:
        active = _active_director(conn)
        if active is not None and active["session_id"] != sid:
            raise builders.Conflict(f"session {active['session_id']} is the registered director; it (or the owner) "
                                    "must run `imperium director release` first")
        tokens.revoke_principal(conn, who)
        if active is None:
            conn.execute("INSERT INTO directors(session_id, claimed_at, claimed_by) VALUES(?,?,?)",
                         (sid, journal.now(), principal))
        raw = tokens.issue(conn, who)
        if conn.execute("SELECT 1 FROM consumers WHERE name='director'").fetchone():
            feeds.reassign(conn, "director", who)  # the feed belongs to the role: unread events carry over
        else:
            feeds.create(conn, "director", principal=who, floor="ACTION")
        journal.append(conn, "DIRECTOR_CLAIMED", "NOTICE", caller=principal, data={"session_id": sid})
    try:
        tokens.write_locator(d.home, who, raw)
    except BaseException:
        with d.store.tx() as conn:
            tokens.revoke_principal(conn, who)
            conn.execute("UPDATE directors SET released_at=? WHERE session_id=? AND released_at IS NULL",
                         (journal.now(), sid))
            journal.append(conn, "DIRECTOR_CLAIM_FAILED", "NOTICE", caller=principal, data={"session_id": sid})
        raise
    return {"director": {"session_id": sid}, "locator": f"tokens/director-{sid}"}


def r_director_release(d, principal, body, query):
    with d.store.tx() as conn:
        active = _active_director(conn)
        if active is None:
            raise feeds.Refused("no director is registered")
        who = "director:" + active["session_id"]
        if principal not in ("owner", who):
            raise feeds.NotYours("only the registered director or the owner can release it")
        tokens.revoke_principal(conn, who)
        conn.execute("UPDATE directors SET released_at=? WHERE id=?", (journal.now(), active["id"]))
        journal.append(conn, "DIRECTOR_RELEASED", "NOTICE", caller=principal,
                       data={"session_id": active["session_id"]})
    tokens.remove_locator(d.home, who)
    return {"released": active["session_id"]}


def r_quarantine_release(d, principal, body, query):
    """The owner accepts a broken journal after inspecting it: the head is anchored, verification restarts
    from it, and the break stays listed in every verification [C6]."""
    reason = str(_req(body, "reason")).strip()
    if not reason:
        raise HttpError(400, "a reason is required")
    with d.store.tx() as conn:
        if not meta_get(conn, "quarantine"):
            raise feeds.Refused("Imperium is not quarantined")
        v = journal.verify_chain(conn)
        head_seq, head_hash = journal.head(conn)
        conn.execute("INSERT INTO anchors(seq, hash, first_bad, reason, ts) VALUES(?,?,?,?,?)",
                     (head_seq, head_hash, v.first_bad, reason, journal.now()))
        conn.execute("UPDATE consumers SET shown_through=MIN(shown_through, ?), acked_seq=MIN(acked_seq, ?)",
                     (head_seq, head_seq))
        meta_set(conn, "quarantine", None)
        journal.append(conn, "QUARANTINE_RELEASED", "NOTICE", caller=principal,
                       data={"anchored_at_seq": head_seq, "first_bad": v.first_bad, "reason": reason})
    d.refresh_chain()
    return {"anchored_at_seq": head_seq}


def r_consumers(d, principal, body, query):
    name, floor = _req(body, "name"), _req(body, "floor")
    with d.store.tx() as conn:
        feeds.create(conn, str(name), principal=principal, floor=str(floor))
    return {"name": name, "floor": floor}


def r_events_since(d, principal, body, query):
    with d.store.tx() as conn:
        return feeds.events_since(conn, str(_req(body, "consumer")), principal,
                                  limit=int(body.get("limit", 50)),
                                  after=_opt_int(body, "after"), high_water=_opt_int(body, "high_water"))


def r_ack(d, principal, body, query):
    with d.store.tx() as conn:
        acked = feeds.ack(conn, str(_req(body, "consumer")), principal, int(_req(body, "seq")))
    return {"acked_seq": acked}


def r_show(d, principal, body, query):
    seq = query.get("seq", [None])[0]
    if seq is None or not seq.isdigit():
        raise HttpError(400, "seq is required")
    with d.store.read() as conn:
        return {"event": feeds.show(conn, int(seq))}


def r_verify(d, principal, body, query):
    c = d.refresh_chain()
    if not c["ok"]:
        d.enter_quarantine(c)
    return {"chain_ok": c["ok"], "checked": c["checked"], "first_bad": c["first_bad"], "reason": c["reason"],
            "checked_through_seq": c["checked_through_seq"], "accepted_breaks": c["accepted_breaks"]}


def r_prune(d, principal, body, query):
    return retention.prune(d.store, paths.archive(d.home), int(_req(body, "through")), caller=principal)


def r_backup(d, principal, body, query):
    dest = body.get("dest")
    if dest:
        dest = backup.check_destination(d.home, str(dest))
    else:
        os.makedirs(paths.backups(d.home), exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(paths.backups(d.home), f"imperium-{stamp}.db")
    path = backup.backup(d.store, dest)
    with d.store.tx() as conn:
        journal.append(conn, "BACKUP_TAKEN", "INFO", caller=principal, data={"path": path})
    return {"path": path}


def r_restore_confirm(d, principal, body, query):
    return {"confirmed": backup.confirm_restore(d.store, by=principal)}


def r_token_rotate(d, principal, body, query):
    tokens.rotate_owner(d.store, d.home)
    return {"rotated": "owner", "locator": "tokens/owner"}


def r_shutdown(d, principal, body, query):
    with d.store.tx() as conn:
        journal.append(conn, "DAEMON_STOPPING", "INFO", caller=principal, data={"run_id": d.run_id})
    threading.Thread(target=d.shutdown, daemon=True).start()
    return {"stopping": True}


ROUTES = {
    ("GET", "/v1/health"): (r_health, None, False),
    ("GET", "/v1/director"): (r_director_show, "any", False),
    ("POST", "/v1/director/claim"): (r_director_claim, "owner", True),
    ("POST", "/v1/director/release"): (r_director_release, "any", True),
    ("POST", "/v1/quarantine/release"): (r_quarantine_release, "owner", True),
    ("GET", "/v1/status"): (r_status, "any", False),
    ("POST", "/v1/consumers"): (r_consumers, "any", True),
    ("GET", "/v1/builders"): (r_builders, "any", False),
    ("POST", "/v1/builders"): (r_builder_add, "owner", True),
    ("POST", "/v1/builders/remove"): (r_builder_remove, "owner", True),
    ("POST", "/v1/events_since"): (r_events_since, "any", True),
    ("POST", "/v1/ack"): (r_ack, "any", True),
    ("GET", "/v1/show"): (r_show, "any", False),
    ("GET", "/v1/verify-journal"): (r_verify, "any", False),
    ("POST", "/v1/prune"): (r_prune, "owner", True),
    ("POST", "/v1/backup"): (r_backup, "owner", True),
    ("POST", "/v1/restore-confirm"): (r_restore_confirm, "owner", True),
    ("POST", "/v1/token/rotate"): (r_token_rotate, "owner", True),
    ("POST", "/v1/shutdown"): (r_shutdown, "owner", True),
}


# State changes still allowed while quarantined: reading feeds (which records what was shown), backups,
# stopping, and the owner's release. Everything that dispatches, decides or changes trust waits [C6].
QUARANTINE_OK = {"/v1/events_since", "/v1/ack", "/v1/consumers", "/v1/backup", "/v1/quarantine/release",
                 "/v1/shutdown"}


def _req(body, key):
    if key not in body:
        raise HttpError(400, f"{key} is required")
    return body[key]


def _opt_int(body, key):
    v = body.get(key)
    return None if v is None else int(v)


# --- HTTP -------------------------------------------------------------------------------------------

class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "imperiumd"
    sys_version = ""

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _send(self, status, payload):
        d = self.server.imperium
        if d.store is not None and d.store.failed:
            payload["critical"] = d.store.failed
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, method):
        d = self.server.imperium
        principal = None
        route = f"{method} {urllib.parse.urlsplit(self.path).path}"
        result = "ok"
        try:
            status, payload, principal = self._handle(d, method)
        except HttpError as e:
            status, payload, result = e.status, {"ok": False, "error": str(e)}, f"http {e.status}"
        except Exception as e:  # never a traceback to the client
            log.exception("unhandled error in %s", route)
            status, payload, result = 500, {"ok": False, "error": f"internal error: {type(e).__name__}"}, "http 500"
        self._send(status, payload)
        if d.store is not None and not d.store.failed and route != "POST /v1/shutdown":
            try:
                with d.store.tx() as conn:
                    audit.record(conn, principal, None, route, result, cap=d.cfg["daemon"]["audit_cap"])
            except (StoreFailed, sqlite3.Error):
                pass

    def _handle(self, d, method):
        allowed = {f"127.0.0.1:{d.port}", f"localhost:{d.port}", f"[::1]:{d.port}"}
        if (self.headers.get("Host") or "").lower() not in allowed:
            raise HttpError(403, "bad Host header")
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() not in {f"http://{a}" for a in allowed}:
            raise HttpError(403, "foreign Origin")
        url = urllib.parse.urlsplit(self.path)
        spec = ROUTES.get((method, url.path))
        if spec is None:
            raise HttpError(404, f"no route {method} {url.path}")
        fn, who, writes = spec
        principal = None
        if who is not None:
            auth = self.headers.get("Authorization") or ""
            raw = auth[7:] if auth.startswith("Bearer ") else ""
            with d.store.read() as conn:
                principal = tokens.verify(conn, raw)
            if principal is None:
                raise HttpError(401, "missing or invalid bearer token")
            if who == "owner" and principal != "owner":
                raise HttpError(403, "owner only")
        if not d.buckets.allow(principal or f"anon:{self.client_address[0]}"):
            raise HttpError(429, "rate limit exceeded; slow down")
        body = self._body(d)
        if writes and d.store.failed:
            raise HttpError(503, d.store.failed)
        if writes and url.path not in QUARANTINE_OK:
            q = d.quarantine()
            if q:
                raise HttpError(503, f"quarantined: {q}; inspect, then `imperium quarantine release` (owner)")
        try:
            out = fn(d, principal, body, urllib.parse.parse_qs(url.query))
        except feeds.NotYours as e:
            raise HttpError(403, str(e)) from None
        except builders.Conflict as e:
            raise HttpError(409, str(e)) from None
        except (feeds.Refused, retention.PruneRefused) as e:
            raise HttpError(409, str(e)) from None
        except StoreFailed as e:
            raise HttpError(503, str(e)) from None
        except (feeds.FeedError, ValueError, TypeError) as e:
            raise HttpError(400, str(e)) from None
        out = dict(out)
        out["ok"] = True
        return 200, out, principal

    def _body(self, d):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        limit = d.cfg["daemon"]["max_body"]
        if length > limit:
            remaining = min(length, 16 * limit)
            while remaining > 0:  # drain so the client sees the reply, not a reset
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            raise HttpError(413, f"body larger than {limit} bytes")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise HttpError(400, "body is not JSON") from None
        if not isinstance(body, dict):
            raise HttpError(400, "body must be a JSON object")
        return body


# --- daemon -----------------------------------------------------------------------------------------

class Daemon:
    def __init__(self, home, run_id=None):
        self.home = home
        self.cfg = config.load(home)
        self.run_id = run_id or secrets.token_hex(8)
        self.pid = os.getpid()
        self.started = journal.now()
        self.store = None
        self.server = None
        self.port = None
        self.chain = None
        self.archives = None
        self._lock = None
        self._stopped = False
        self._stop_lock = threading.Lock()
        self.engine = None
        self.stopped = threading.Event()  # set once every resource is released
        self.buckets = Buckets(self.cfg["daemon"]["rate_per_sec"], self.cfg["daemon"]["burst"])

    def start(self, port=0):
        self._lock = acquire_lock(self.home)
        try:
            self.store = Store(paths.db(self.home))
            self._recover()
            self.server = _Server(("127.0.0.1", port), _Handler)
            self.server.imperium = self
            self.port = self.server.server_address[1]
            self._write_info()
            self.engine = Engine(self)
            self.engine.start()
        except BaseException:
            if self.store is not None:
                self.store.close()
            release_lock(self._lock)
            raise
        log.info("imperiumd %s listening on 127.0.0.1:%s (run %s)", __version__, self.port, self.run_id)
        return self

    def quarantine(self):
        with self.store.read() as conn:
            return meta_get(conn, "quarantine")

    def refresh_chain(self):
        """Verify the chain now and record when, and through which event, it was checked [C16]."""
        with self.store.read() as conn:
            r = journal.verify_chain(conn)
            head_seq, _ = journal.head(conn)
            breaks = journal.accepted_breaks(conn)
        self.chain = {"ok": r.ok, "checked": r.checked, "first_bad": r.first_bad, "reason": r.reason,
                      "checked_at": journal.now(), "checked_through_seq": head_seq, "accepted_breaks": breaks}
        return self.chain

    def enter_quarantine(self, chain):
        """A break found while running quarantines exactly like one found at start-up [C6]."""
        with self.store.tx() as conn:
            if meta_get(conn, "quarantine"):
                return
            journal.append(conn, "INTEGRITY_FAIL", "CRITICAL",
                           data={"first_bad": chain["first_bad"], "reason": chain["reason"]})
            meta_set(conn, "quarantine", f"journal chain broken at event {chain['first_bad']}: {chain['reason']}")
            journal.append(conn, "QUARANTINED", "CRITICAL", data={"reason": "journal chain broken"})

    def _recover(self):
        r = self.refresh_chain()
        self.archives = retention.check_archives(self.store, paths.archive(self.home))
        with self.store.tx() as conn:
            tokens.finish_rotation(conn, self.home)
            if not r["ok"]:
                # An untrustworthy journal stops everything that acts; reading stays possible [C6].
                journal.append(conn, "INTEGRITY_FAIL", "CRITICAL",
                               data={"first_bad": r["first_bad"], "reason": r["reason"]})
                meta_set(conn, "quarantine", f"journal chain broken at event {r['first_bad']}: {r['reason']}")
                journal.append(conn, "QUARANTINED", "CRITICAL", data={"reason": "journal chain broken"})
            journal.append(conn, "DAEMON_STARTED", "INFO",
                           data={"run_id": self.run_id, "pid": self.pid, "version": __version__})
            journal.append(conn, "RECOVERED", "INFO",
                           data={"chain_ok": r["ok"], "archives_removed": len(self.archives["removed"]),
                                 "archives_missing": len(self.archives["missing"]),
                                 "archives_mismatched": len(self.archives["mismatched"]),
                                 "observe_only": meta_get(conn, "observe_only")})

    def _write_info(self):
        info = {"port": self.port, "pid": self.pid, "run_id": self.run_id, "started": self.started,
                "version": __version__}
        fsutil.atomic_write(paths.daemon_json(self.home), json.dumps(info), mode=0o600)

    def serve_forever(self):
        self.server.serve_forever(poll_interval=0.2)

    def shutdown(self):
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        self.server.shutdown()
        self.server.server_close()
        if self.engine:
            self.engine.stop()
        try:
            with open(paths.daemon_json(self.home), encoding="utf-8") as f:
                if json.load(f).get("run_id") == self.run_id:
                    os.remove(paths.daemon_json(self.home))
        except (OSError, ValueError):
            pass
        self.store.close()
        release_lock(self._lock)
        self.stopped.set()
        log.info("imperiumd stopped (run %s)", self.run_id)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="imperiumd")
    ap.add_argument("--home", required=True)
    ap.add_argument("--run-id", help="chosen by `imperium up`, which waits for this id to answer")
    args = ap.parse_args(argv)
    os.makedirs(paths.logs(args.home), exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(os.path.join(paths.logs(args.home), "imperiumd.log"),
                                                   maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    try:
        d = Daemon(args.home, run_id=args.run_id).start()
    except AlreadyRunning as e:
        log.error("%s", e)
        return 3
    except Exception:
        log.exception("start failed")
        return 1
    d.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
