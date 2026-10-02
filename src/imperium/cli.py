"""`imperium` command line (DESIGN §11.1). `--json` everywhere, documented exit codes, no tracebacks.

Exit codes: 0 ok · 1 error · 2 usage · 3 daemon not running · 4 refused · 5 integrity or doctor failure.
"""
import argparse
import datetime
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.parse

from . import __version__, backup, config, feeds, journal, paths, retention, tokens
from .client import ApiError, Client, DaemonDown
from .daemon import AlreadyRunning, acquire_lock, release_lock
from .store import Store

OK, ERROR, USAGE, DOWN, REFUSED, INTEGRITY = 0, 1, 2, 3, 4, 5


class Usage(Exception):
    pass


class Fail(Exception):
    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


class _Parser(argparse.ArgumentParser):
    def __init__(self, *a, err=None, **kw):
        super().__init__(*a, **kw)
        self._err = err

    def error(self, message):
        raise Usage(f"{self.prog}: {message}")

    def _print_message(self, message, file=None):
        if message:
            (self._err or sys.stderr).write(message)


def _parser(err):
    p = _Parser(prog="imperium", description="Imperium: deliver prompts exactly once, record every event, "
                "verify builders' claims.", err=err)
    p.add_argument("--home", help="runtime directory (default: IMPERIUM_HOME or ~/.imperium)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--as", dest="as_who", choices=["owner"], help="act as the owner inside a Claude Code session")
    p.add_argument("--version", action="version", version=f"imperium {__version__}")
    sub = p.add_subparsers(dest="cmd", parser_class=_Parser)
    sub.required = True

    def add(name, help_):
        return sub.add_parser(name, help=help_, err=err)

    add("init", "create the runtime directory, database, config and owner token (idempotent)")
    add("up", "start the daemon in the background (idempotent)")
    add("down", "stop the daemon")
    add("status", "daemon, journal and feed status")
    d = add("doctor", "check the installation")
    d.add_argument("--contract", action="store_true", help="also print a reproducible setup report")
    e = add("events", "read your feed from its bookmark up to the high-water mark")
    e.add_argument("--consumer")
    e.add_argument("--page-size", type=int, default=50)
    e.add_argument("--max", type=int, default=1000, help="stop after this many events")
    a = add("ack", "mark your feed read through SEQ (only what was shown)")
    a.add_argument("seq", type=int)
    a.add_argument("--consumer")
    s = add("show", "show one event in full (moves no bookmark)")
    s.add_argument("seq", type=int)
    add("verify-journal", "check the journal's integrity chain")
    pr = add("prune", "archive and delete the oldest events (owner)")
    pr.add_argument("--through", type=int, required=True)
    b = add("backup", "back up the database with SQLite's online backup API (owner)")
    b.add_argument("--to")
    r = add("restore", "restore a backup; the daemon must be stopped (owner)")
    r.add_argument("path")
    r.add_argument("--force", action="store_true",
                   help="restore a backup that fails Imperium's checks, into quarantine")
    add("restore-confirm", "end observe-only mode after a restore (owner)")
    bl = add("builder", "register and inspect builders")
    bsub = bl.add_subparsers(dest="builder_cmd", parser_class=_Parser)
    bsub.required = True
    ba = bsub.add_parser("add", help="register an OpenCode session as a builder (owner)", err=err)
    ba.add_argument("name")
    ba.add_argument("--endpoint", required=True, help="the OpenCode server, e.g. http://127.0.0.1:<port>")
    ba.add_argument("--session", required=True, help="the OpenCode session id (ses_...)")
    ba.add_argument("--directory", required=True, help="the session's workspace directory")
    g = ba.add_mutually_exclusive_group()
    g.add_argument("--password-env", help="name of the variable holding the server password")
    g.add_argument("--password-file", help="file holding the server password")
    ba.add_argument("--no-check", action="store_true", help="do not contact the server first")
    bsub.add_parser("list", help="list builders", err=err)
    bs = bsub.add_parser("show", help="show one builder", err=err)
    bs.add_argument("name")
    br = bsub.add_parser("remove", help="unregister a builder (owner)", err=err)
    br.add_argument("name")
    dr = add("director", "register the Claude Code session that directs builders")
    dsub = dr.add_subparsers(dest="director_cmd", parser_class=_Parser)
    dsub.required = True
    dsub.add_parser("claim", help="register this Claude Code session as the director (owner: --as owner)", err=err)
    dsub.add_parser("release", help="unregister the director (the director itself or the owner)", err=err)
    dsub.add_parser("show", help="show the registered director", err=err)
    qa = add("quarantine", "inspect or end quarantine after a journal integrity failure")
    qsub = qa.add_subparsers(dest="quarantine_cmd", parser_class=_Parser)
    qsub.required = True
    qr = qsub.add_parser("release", help="accept the broken journal after inspection (owner)", err=err)
    qr.add_argument("--reason", required=True, help="why the owner accepts it; recorded in the journal")
    t = add("token", "manage tokens (owner)")
    t.add_argument("action", choices=["rotate"])
    return p


def main(argv=None, out=None, err=None, env=None):
    out = out or sys.stdout
    err = err or sys.stderr
    env = os.environ if env is None else env
    try:
        args = _parser(err).parse_args(argv)
    except Usage as e:
        err.write(f"{e}\n")
        return USAGE
    except SystemExit as e:  # --help / --version
        return int(e.code or 0)
    home = paths.home(args.home, env)
    ctx = {"home": home, "env": env, "as_owner": args.as_who == "owner", "args": args}
    try:
        result = COMMANDS[args.cmd](ctx)
        code = result.pop("_code", OK)
    except Fail as e:
        result, code = {"ok": False, "error": str(e), **e.extra}, e.code
    except DaemonDown as e:
        result, code = {"ok": False, "error": str(e)}, DOWN
    except ApiError as e:
        code = REFUSED if e.status in (401, 403, 409, 429) else ERROR
        result = {"ok": False, "error": str(e), "http_status": e.status}
    except tokens.NoCredential as e:
        result, code = {"ok": False, "error": str(e)}, REFUSED
    except config.ConfigError as e:
        result, code = {"ok": False, "error": str(e)}, ERROR
    except Exception as e:  # no tracebacks
        result, code = {"ok": False, "error": f"{type(e).__name__}: {e}"}, ERROR
    result.setdefault("ok", code == OK)
    if args.json:
        out.write(json.dumps(result, ensure_ascii=False, indent=None) + "\n")
    else:
        _human(args.cmd, result, out, err)
    return code


# --- helpers ----------------------------------------------------------------------------------------

def _client(ctx):
    return Client(ctx["home"], as_owner=ctx["as_owner"], env=ctx["env"])


def _owner_client(ctx):
    return Client(ctx["home"], as_owner=True, env=ctx["env"])


def _with_lock(ctx, what):
    if not os.path.isdir(ctx["home"]):
        raise Fail(ERROR, f"{ctx['home']} does not exist; run `imperium init`")
    try:
        return acquire_lock(ctx["home"])
    except AlreadyRunning:
        raise Fail(REFUSED, f"the daemon is running; {what} needs it stopped (`imperium down`)") from None


def _open_readonly(home):
    path = paths.db(home)
    if not os.path.exists(path):
        raise Fail(ERROR, f"no database at {path}; run `imperium init`")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _default_consumer(ctx, c):
    return "owner" if c.credential()[0] == "owner" else "director"


# --- commands ---------------------------------------------------------------------------------------

def cmd_init(ctx):
    home = ctx["home"]
    created = not os.path.exists(paths.db(home))
    paths.ensure_home(home)
    lock = _with_lock(ctx, "init")
    try:
        if not os.path.exists(paths.config(home)):
            with open(paths.config(home), "w", encoding="utf-8") as f:
                f.write(config.DEFAULT_TEXT)
        config.load(home)
        store = Store(paths.db(home))
        try:
            with store.tx() as conn:
                raw = tokens.read_locator(home, "owner")
                if not raw or tokens.verify(conn, raw) != "owner":
                    tokens.revoke_principal(conn, "owner")
                    raw = tokens.issue(conn, "owner")
                    tokens.write_locator(home, "owner", raw)
                feeds.create(conn, "owner", principal="owner", floor="NOTICE")
                if journal.head(conn)[0] == 0:
                    journal.append(conn, "INITIALIZED", "INFO", caller="owner", data={"version": __version__})
        finally:
            store.close()
    finally:
        release_lock(lock)
    return {"home": home, "created": created}


def cmd_up(ctx):
    c = _owner_client(ctx)
    h = c.health()
    if h:
        return {"run_id": h["run_id"], "pid": h["pid"], "already_running": True}
    if not os.path.exists(paths.db(ctx["home"])):
        raise Fail(ERROR, "not initialised; run `imperium init`")
    os.makedirs(paths.logs(ctx["home"]), exist_ok=True)
    # Wait for our own run id, not a process id: on Windows a virtual-environment python.exe is a
    # launcher that starts the real interpreter as a child process with another pid.
    run_id = secrets.token_hex(8)
    cmd = [sys.executable, "-m", "imperium.daemon", "--home", ctx["home"], "--run-id", run_id]
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
          "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kw)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        h = c.health()
        if h and h["run_id"] == run_id:
            return {"run_id": h["run_id"], "pid": h["pid"], "already_running": False}
        rc = proc.poll()
        if rc is not None:
            if rc == 3:  # another daemon won the race
                h = c.health()
                if h:
                    return {"run_id": h["run_id"], "pid": h["pid"], "already_running": True}
            raise Fail(ERROR, f"the daemon exited with code {rc}; see {paths.logs(ctx['home'])}")
        time.sleep(0.1)
    raise Fail(ERROR, f"the daemon did not answer within 15 s; see {paths.logs(ctx['home'])}")


def cmd_down(ctx):
    c = _client(ctx)
    if not c.health():
        return {"stopped": False, "note": "the daemon was not running"}
    c.call("POST", "/v1/shutdown", {})
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not c.health():
            return {"stopped": True}
        time.sleep(0.1)
    raise Fail(ERROR, "the daemon did not stop within 10 s")


def cmd_status(ctx):
    return _client(ctx).call("GET", "/v1/status")


def cmd_events(ctx):
    a = ctx["args"]
    c = _client(ctx)
    consumer = a.consumer or _default_consumer(ctx, c)
    events, after, hw, urgent = [], None, None, None
    while len(events) < a.max:
        body = {"consumer": consumer, "limit": min(a.page_size, a.max - len(events))}
        if after is not None:
            body["after"] = after
        if hw is not None:
            body["high_water"] = hw
        r = c.call("POST", "/v1/events_since", body)
        hw, urgent = r["high_water"], r["urgent"]
        events.extend(r["events"])
        if not r["more"] or not r["events"]:
            break
        after = r["events"][-1]["seq"]
    return {"consumer": consumer, "events": events, "high_water": hw, "urgent": urgent,
            "complete": len(events) < a.max}


def cmd_ack(ctx):
    a = ctx["args"]
    c = _client(ctx)
    consumer = a.consumer or _default_consumer(ctx, c)
    return c.call("POST", "/v1/ack", {"consumer": consumer, "seq": a.seq})


def cmd_show(ctx):
    return _client(ctx).call("GET", f"/v1/show?seq={ctx['args'].seq}")


def cmd_verify(ctx):
    c = _client(ctx)
    if c.health():
        r = c.call("GET", "/v1/verify-journal")
        res = {"ok": r["chain_ok"], "checked": r["checked"], "first_bad": r["first_bad"], "reason": r["reason"],
               "via": "daemon"}
    else:
        conn = _open_readonly(ctx["home"])
        try:
            v = journal.verify_chain(conn)
        finally:
            conn.close()
        res = {"ok": v.ok, "checked": v.checked, "first_bad": v.first_bad, "reason": v.reason, "via": "offline"}
    if not res["ok"]:
        res["_code"] = INTEGRITY
    return res


def cmd_prune(ctx):
    return _owner_or_refuse(ctx).call("POST", "/v1/prune", {"through": ctx["args"].through})


def cmd_backup(ctx):
    a = ctx["args"]
    c = _owner_or_refuse(ctx)
    body = {"dest": os.path.abspath(a.to)} if a.to else {}
    if c.health():
        return c.call("POST", "/v1/backup", body)
    lock = _with_lock(ctx, "an offline backup")
    try:
        store = Store(paths.db(ctx["home"]))
        try:
            dest = body.get("dest")
            if dest:
                try:
                    dest = backup.check_destination(ctx["home"], dest)
                except ValueError as e:
                    raise Fail(ERROR, str(e)) from None
            else:
                os.makedirs(paths.backups(ctx["home"]), exist_ok=True)
                dest = os.path.join(paths.backups(ctx["home"]),
                                    f"imperium-{datetime.datetime.now():%Y%m%d-%H%M%S}.db")
            return {"path": backup.backup(store, dest), "via": "offline"}
        finally:
            store.close()
    finally:
        release_lock(lock)


def cmd_restore(ctx):
    _owner_or_refuse(ctx)
    lock = _with_lock(ctx, "restore")
    try:
        backup.restore(os.path.abspath(ctx["args"].path), paths.db(ctx["home"]), force=ctx["args"].force)
    except backup.RestoreError as e:
        raise Fail(ERROR, str(e)) from None
    finally:
        release_lock(lock)
    return {"restored": ctx["args"].path, "observe_only": "restored",
            "next": "start the daemon, check the builders' history, then `imperium restore-confirm`"}


def cmd_restore_confirm(ctx):
    return _owner_or_refuse(ctx).call("POST", "/v1/restore-confirm", {})


def cmd_builder(ctx):
    a = ctx["args"]
    if a.builder_cmd == "list":
        return _client(ctx).call("GET", "/v1/builders")
    if a.builder_cmd == "show":
        return _client(ctx).call("GET", "/v1/builders?name=" + urllib.parse.quote(a.name))
    c = _owner_or_refuse(ctx)
    if a.builder_cmd == "remove":
        return c.call("POST", "/v1/builders/remove", {"name": a.name})
    body = {"name": a.name, "endpoint": a.endpoint, "session_id": a.session, "directory": a.directory,
            "check": not a.no_check}
    if a.password_env:
        body["password_env"] = a.password_env
    if a.password_file:
        body["password_file"] = os.path.abspath(a.password_file)
    return c.call("POST", "/v1/builders", body)


def cmd_director(ctx):
    a = ctx["args"]
    if a.director_cmd == "show":
        return _client(ctx).call("GET", "/v1/director")
    if a.director_cmd == "release":
        return _client(ctx).call("POST", "/v1/director/release", {})
    sid = ctx["env"].get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        raise Fail(ERROR, "run this inside the Claude Code session that will direct: CLAUDE_CODE_SESSION_ID is not "
                   "set here")
    return _owner_or_refuse(ctx).call("POST", "/v1/director/claim", {"session_id": sid})


def cmd_quarantine(ctx):
    return _owner_or_refuse(ctx).call("POST", "/v1/quarantine/release", {"reason": ctx["args"].reason})


def cmd_token(ctx):
    return _owner_or_refuse(ctx).call("POST", "/v1/token/rotate", {})


def _owner_or_refuse(ctx):
    c = _client(ctx)
    if c.credential()[0] != "owner":
        raise Fail(REFUSED, "owner only; the owner runs this outside Claude Code or with --as owner")
    return c


def cmd_doctor(ctx):
    home = ctx["home"]
    checks = []

    def check(name, status, detail):
        checks.append({"name": name, "status": status, "detail": detail})

    v = sys.version_info
    check("python", "OK" if v >= (3, 11) else "FAIL", f"{sys.executable} {v.major}.{v.minor}.{v.micro}")
    if not os.path.isdir(home):
        check("home", "FAIL", f"{home} does not exist; run `imperium init`")
        return _doctor_result(checks, ctx)
    check("home", "OK", home)
    check("home private", *paths.check_private(home))
    check("home local", *paths.check_local(home))
    try:
        config.load(home)
        check("config", "OK", paths.config(home))
    except config.ConfigError as e:
        check("config", "FAIL", str(e))
    conn = None
    try:
        conn = _open_readonly(home)
        ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
        check("database", "OK" if ic == "ok" else "FAIL", f"SQLite integrity_check: {ic}")
        r = journal.verify_chain(conn)
        check("journal chain", "OK" if r.ok else "FAIL",
              f"{r.checked} events checked" if r.ok else f"first bad event {r.first_bad}: {r.reason}")
        logged = {x["archive"]: x["archive_sha256"] for x in conn.execute("SELECT * FROM prune_log")}
        missing = [n for n in logged if not os.path.exists(os.path.join(paths.archive(home), n))]
        bad = [n for n, h in logged.items() if n not in missing
               and retention.sha256_file(os.path.join(paths.archive(home), n)) != h]
        check("archives", "FAIL" if (missing or bad) else "OK",
              f"{len(logged)} archives; missing {missing}; altered {bad}")
        raw = tokens.read_locator(home, "owner")
        check("owner token", "OK" if raw and tokens.verify(conn, raw) == "owner" else "FAIL",
              "tokens/owner matches the database" if raw else "tokens/owner is missing; run `imperium init`")
        newest = conn.execute("SELECT ts FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        if newest:
            ts = datetime.datetime.fromisoformat(newest[0])
            skew = (ts - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
            check("clock", "WARN" if skew > 300 else "OK",
                  f"newest event is {skew:.0f} s in the future" if skew > 300 else "newest event is not in the future")
    except Fail as e:
        check("database", "FAIL", str(e))
    finally:
        if conn is not None:
            conn.close()
    h = Client(home, as_owner=True, env=ctx["env"]).health()
    check("daemon", "OK" if h else "WARN", f"running, pid {h['pid']}" if h else "not running (`imperium up`)")
    return _doctor_result(checks, ctx)


def _doctor_result(checks, ctx):
    res = {"checks": checks}
    if ctx["args"].contract:
        home = ctx["home"]
        res["contract"] = {
            "imperium": __version__, "python": sys.executable, "home": home, "database": paths.db(home),
            "config": paths.config(home), "credential_locations": ["tokens/owner", "tokens/director-<session id>"],
            "daemon_command": [sys.executable, "-m", "imperium.daemon", "--home", home],
        }
    if any(c["status"] == "FAIL" for c in checks):
        res["_code"] = INTEGRITY
    return res


COMMANDS = {
    "init": cmd_init, "up": cmd_up, "down": cmd_down, "status": cmd_status, "doctor": cmd_doctor,
    "events": cmd_events, "ack": cmd_ack, "show": cmd_show, "verify-journal": cmd_verify, "prune": cmd_prune,
    "backup": cmd_backup, "restore": cmd_restore, "restore-confirm": cmd_restore_confirm, "token": cmd_token,
    "builder": cmd_builder, "director": cmd_director, "quarantine": cmd_quarantine,
}


# --- human output -----------------------------------------------------------------------------------

def _human(cmd, r, out, err):
    if not r.get("ok", False) and "error" in r:
        err.write(f"error: {r['error']}\n")
        if cmd not in ("doctor", "verify-journal"):
            return
    if cmd == "events":
        for e in r.get("events", []):
            out.write(e["headline"] + "\n")
        u = r.get("urgent") or {}
        if u.get("count"):
            out.write(f"URGENT: {u['count']} unacknowledged CRITICAL: "
                      + ", ".join(f"#{i['seq']}" for i in u["items"]) + "\n")
        out.write(f"(through #{r.get('high_water')}; `imperium ack {r.get('high_water')}` when read)\n")
    elif cmd == "doctor":
        for c in r.get("checks", []):
            out.write(f"{c['status']:<10} {c['name']:<14} {c['detail']}\n")
        if "contract" in r:
            out.write(json.dumps(r["contract"], indent=2) + "\n")
    elif cmd == "status":
        out.write(f"imperiumd {r.get('version')} pid {r.get('pid')} port {r.get('port')} run {r.get('run_id')}\n")
        out.write(f"journal head #{r.get('head_seq')}; chain {'ok' if (r.get('chain') or {}).get('ok') else 'BROKEN'}\n")
        if r.get("critical"):
            out.write(f"CRITICAL: {r['critical']}\n")
        if r.get("observe_only"):
            out.write(f"OBSERVE-ONLY ({r['observe_only']}): run `imperium restore-confirm` when checked\n")
        out.write(f"needs you: {r.get('needs_you', 0)} unacknowledged ACTION/CRITICAL\n")
    elif cmd == "show":
        out.write(json.dumps(r.get("event"), ensure_ascii=True, indent=2) + "\n")
    else:
        shown = {k: v for k, v in r.items() if k != "ok"}
        out.write((json.dumps(shown, ensure_ascii=True) if shown else "ok") + "\n")
