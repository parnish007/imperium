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

from . import __version__, audit, backup, config, feeds, journal, paths, retention, tokens
from .store import Store, StoreFailed, meta_get

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
    return {"run_id": d.run_id, "pid": d.pid, "port": d.port, "started": d.started, "version": __version__,
            "principal": principal, "head_seq": head_seq, "chain": d.chain, "archives": d.archives,
            "observe_only": observe_only, "consumers": consumers, "needs_you": needs_you,
            "audit_dropped": dropped}


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
    with d.store.read() as conn:
        r = journal.verify_chain(conn)
    return {"chain_ok": r.ok, "checked": r.checked, "first_bad": r.first_bad, "reason": r.reason}


def r_prune(d, principal, body, query):
    return retention.prune(d.store, paths.archive(d.home), int(_req(body, "through")), caller=principal)


def r_backup(d, principal, body, query):
    dest = body.get("dest")
    if not dest:
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
    with d.store.tx() as conn:
        tokens.revoke_principal(conn, "owner")
        raw = tokens.issue(conn, "owner")
        tokens.write_locator(d.home, "owner", raw)  # inside the transaction: a failed write rolls back
        journal.append(conn, "TOKEN_ROTATED", "NOTICE", caller=principal, data={"principal": "owner"})
    return {"rotated": "owner", "locator": "tokens/owner"}


def r_shutdown(d, principal, body, query):
    with d.store.tx() as conn:
        journal.append(conn, "DAEMON_STOPPING", "INFO", caller=principal, data={"run_id": d.run_id})
    threading.Thread(target=d.shutdown, daemon=True).start()
    return {"stopping": True}


ROUTES = {
    ("GET", "/v1/health"): (r_health, None, False),
    ("GET", "/v1/status"): (r_status, "any", False),
    ("POST", "/v1/consumers"): (r_consumers, "any", True),
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
        try:
            out = fn(d, principal, body, urllib.parse.parse_qs(url.query))
        except feeds.NotYours as e:
            raise HttpError(403, str(e)) from None
        except (feeds.Refused, retention.PruneRefused, sqlite3.IntegrityError) as e:
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
        except BaseException:
            if self.store is not None:
                self.store.close()
            release_lock(self._lock)
            raise
        log.info("imperiumd %s listening on 127.0.0.1:%s (run %s)", __version__, self.port, self.run_id)
        return self

    def _recover(self):
        with self.store.read() as conn:
            r = journal.verify_chain(conn)
        self.chain = {"ok": r.ok, "checked": r.checked, "first_bad": r.first_bad, "reason": r.reason}
        self.archives = retention.check_archives(self.store, paths.archive(self.home))
        with self.store.tx() as conn:
            if not r.ok:
                journal.append(conn, "INTEGRITY_FAIL", "CRITICAL",
                               data={"first_bad": r.first_bad, "reason": r.reason})
            journal.append(conn, "DAEMON_STARTED", "INFO",
                           data={"run_id": self.run_id, "pid": self.pid, "version": __version__})
            journal.append(conn, "RECOVERED", "INFO",
                           data={"chain_ok": r.ok, "archives_removed": len(self.archives["removed"]),
                                 "archives_missing": len(self.archives["missing"]),
                                 "archives_mismatched": len(self.archives["mismatched"]),
                                 "observe_only": meta_get(conn, "observe_only")})

    def _write_info(self):
        info = {"port": self.port, "pid": self.pid, "run_id": self.run_id, "started": self.started,
                "version": __version__}
        tmp = paths.daemon_json(self.home) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(info, f)
        os.replace(tmp, paths.daemon_json(self.home))

    def serve_forever(self):
        self.server.serve_forever(poll_interval=0.2)

    def shutdown(self):
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        self.server.shutdown()
        self.server.server_close()
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
