"""`imperium` command line (DESIGN §11.1). `--json` everywhere, documented exit codes, no tracebacks.

Exit codes: 0 ok · 1 error · 2 usage · 3 daemon not running · 4 refused · 5 integrity or doctor failure.
"""
import argparse
import datetime
import json
import os
import secrets
import shutil
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
    p = _Parser(prog="imperium", description="Imperium: record delivery evidence, expose uncertain outcomes, "
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
    ba = bsub.add_parser("add", help="register a builder (owner): an OpenCode session, or an ACP agent Imperium "
                                     "starts", err=err)
    ba.add_argument("name")
    ba.add_argument("--endpoint", help="OpenCode: the server, e.g. http://127.0.0.1:<port>")
    ba.add_argument("--session", help="OpenCode: the session id (ses_...)")
    ba.add_argument("--acp", metavar="COMMAND",
                    help="an Agent Client Protocol agent Imperium starts and owns, e.g. \"opencode acp\" or a JSON "
                         "list; it gets a new session in --directory")
    ba.add_argument("--directory", required=True, help="the builder's workspace directory")
    g = ba.add_mutually_exclusive_group()
    g.add_argument("--password-env", help="name of the variable holding the server password")
    g.add_argument("--password-file", help="file holding the server password")
    ba.add_argument("--no-check", action="store_true", help="do not contact the server first")
    bsub.add_parser("list", help="list builders", err=err)
    bs = bsub.add_parser("show", help="show one builder", err=err)
    bs.add_argument("name")
    br = bsub.add_parser("remove", help="unregister a builder (owner)", err=err)
    br.add_argument("name")
    bp = bsub.add_parser("pause", help="send only the owner's messages to this builder (owner)", err=err)
    bp.add_argument("name")
    bu = bsub.add_parser("resume", help="send everyone's messages to this builder again (owner)", err=err)
    bu.add_argument("name")
    bv = bsub.add_parser("allow-version", help="accept an untested OpenCode version for this builder (owner)",
                         err=err)
    bv.add_argument("name")
    bv.add_argument("version")
    se = add("send", "queue a message for a builder; it is sent once the builder is ready")
    se.add_argument("builder")
    src = se.add_mutually_exclusive_group(required=True)
    src.add_argument("--message", help="the message text")
    src.add_argument("--file", help="read the message text from this file")
    se.add_argument("--key", help="idempotency key: sending again with the same key and text does nothing new "
                    "(default: a fresh key, printed)")
    se.add_argument("--needs-resources", action="store_true", help="wait until `imperium gate` says OK")
    qu = add("queue", "messages queued or in flight")
    qu.add_argument("builder", nargs="?")
    qu.add_argument("--all", action="store_true", help="include settled messages")
    ms = add("msg", "inspect or decide on one message")
    msub = ms.add_subparsers(dest="msg_cmd", parser_class=_Parser)
    msub.required = True
    mshow = msub.add_parser("show", help="show a message and its state", err=err)
    mshow.add_argument("id")
    mshow.add_argument("--body", action="store_true", help="include the text")
    mcan = msub.add_parser("cancel", help="withdraw a message (a copy already inside the builder stays watched)",
                           err=err)
    mcan.add_argument("id")
    mres = msub.add_parser("resolve", help="decide on an UNCERTAIN or STRANDED message", err=err)
    mres.add_argument("id")
    mres.add_argument("choice", choices=["wait", "cancel", "resend"])
    mres.add_argument("--confirm-may-run-twice", action="store_true",
                      help="required for resend: the original may still run as well")
    bt = bsub.add_parser("token", help="issue a new token for the builder's MCP tool (owner)", err=err)
    bt.add_argument("name")
    bm = bsub.add_parser("mcp-config", help="print (or --write) the OpenCode config that adds Imperium's builder "
                         "tool", err=err)
    bm.add_argument("name")
    bm.add_argument("--write", action="store_true", help="merge it into opencode.json in the builder's workspace "
                    "(takes effect when OpenCode restarts; restart it while the builder is idle)")
    bm.add_argument("--isolated", metavar="FILE",
                    help="owner, isolation mode: write a standalone OpenCode config for a builder running under its "
                         "own account (a fresh builder token and the builder pipe, nothing else); start that "
                         "builder's OpenCode with OPENCODE_CONFIG=FILE")
    ro = add("round", "give a builder one objective and follow it to a decision")
    rsub = ro.add_subparsers(dest="round_cmd", parser_class=_Parser)
    rsub.required = True
    rop = rsub.add_parser("open", help="open a round: the brief is queued for the builder", err=err)
    rop.add_argument("builder")
    g2 = rop.add_mutually_exclusive_group(required=True)
    g2.add_argument("--objective")
    g2.add_argument("--objective-file")
    rop.add_argument("--key", help="idempotency key (default: a fresh one, printed)")
    rl = rsub.add_parser("list", help="open rounds (--all for decided ones too)", err=err)
    rl.add_argument("builder", nargs="?")
    rl.add_argument("--all", action="store_true")
    rs = rsub.add_parser("show", help="one round with its messages, claims and check runs", err=err)
    rs.add_argument("id")
    rm = rsub.add_parser("message", help="send a repair or continue message within the round", err=err)
    rm.add_argument("id")
    g3 = rm.add_mutually_exclusive_group(required=True)
    g3.add_argument("--message")
    g3.add_argument("--file")
    rm.add_argument("--key")
    rv = rsub.add_parser("verify", help="snapshot the code and run the round's trusted checks", err=err)
    rv.add_argument("id")
    rv.add_argument("--wait", type=float, default=0, metavar="SECONDS", help="wait for the result")
    rob = rsub.add_parser("objective", help="record whether the result meets the objective", err=err)
    rob.add_argument("id")
    rob.add_argument("verdict", choices=["met", "not-met"])
    rob.add_argument("--note", default="")
    rd = rsub.add_parser("diff", help="what changed since the round began", err=err)
    rd.add_argument("id")
    for verb in ("accept", "reject", "abandon"):
        x = rsub.add_parser(verb, help=f"{verb} the round (a principal's decision; the first one wins)", err=err)
        x.add_argument("id")
        x.add_argument("--note", default="")
        if verb == "accept":
            x.add_argument("--override", action="store_true", help="owner: accept although not VERIFIED or the "
                           "workspace changed (journaled as an override)")
    ck = add("check", "trusted checks: commands Imperium runs to verify a round")
    csub = ck.add_subparsers(dest="check_cmd", parser_class=_Parser)
    csub.required = True
    ca = csub.add_parser("add", help="define a check: imperium check add ID --builder B -- pytest -q tests", err=err)
    ca.add_argument("id")
    g4 = ca.add_mutually_exclusive_group(required=True)
    g4.add_argument("--builder")
    g4.add_argument("--round")
    ca.add_argument("--dir", default=".", help="working directory, relative to the builder's directory")
    ca.add_argument("--env", action="append", default=[], help="environment variable passed through (repeatable)")
    ca.add_argument("--timeout", type=float, default=1800)
    ca.add_argument("--must-fail-on-base", action="store_true",
                    help="it must fail on the code before the round, or it does not test the change")
    ca.add_argument('--base-failure-code', type=int, action='append',
                    help='expected test-failure exit code on base (repeatable, default: 1; never timeout/setup error)')
    ca.add_argument("--depends", action="append", default=[],
                    help="a file the check relies on (test, script); a change to it makes the check untrusted")
    ca.add_argument("--optional", action="store_true", help="report it, but do not require it to pass")
    ca.add_argument("argv", nargs="+", help="the command, after --")
    cl = csub.add_parser("list", help="checks", err=err)
    cl.add_argument("--builder")
    cl.add_argument("--round")
    cp = csub.add_parser("approve", help="owner: accept the current versions of a check's files", err=err)
    cp.add_argument("id")
    cr = csub.add_parser("retire", help="owner: stop using a check", err=err)
    cr.add_argument("id")
    bmcp = add("builder-mcp", "run the builder's MCP tool on stdio (started by OpenCode, not by hand)")
    bmcp.add_argument("--builder", required=True)
    ap = add("approvals", "permission asks waiting for a decision (--auto: the automatic answers report)")
    ap.add_argument("--all", action="store_true", help="include decided ones")
    ap.add_argument("--auto", action="store_true", help="owner: answers given by your rules; marks them reported")
    ap.add_argument("--builder")
    av = add("approve", "allow a permission ask")
    av.add_argument("id")
    av.add_argument("--always", action="store_true", help="owner: allow this pattern for the rest of the session")
    av.add_argument("--note", default="")
    dn = add("deny", "refuse a permission ask")
    dn.add_argument("id")
    dn.add_argument("--note", default="")
    ru = add("rule", "the owner's approval rules (used only while the director is present)")
    rusub = ru.add_subparsers(dest="rule_cmd", parser_class=_Parser)
    rusub.required = True
    rua = rusub.add_parser("add", help="owner: add a rule", err=err)
    rua.add_argument("permission", help="bash, edit, read, webfetch, ... or *")
    rua.add_argument("pattern", help="glob over the ask's patterns, e.g. 'git status*'")
    g5 = rua.add_mutually_exclusive_group(required=True)
    g5.add_argument("--allow", action="store_true")
    g5.add_argument("--deny", action="store_true")
    rua.add_argument("--path-under", help="only paths inside this absolute directory")
    rua.add_argument("--builder")
    rusub.add_parser("list", help="rules", err=err)
    rur = rusub.add_parser("remove", help="owner: remove a rule", err=err)
    rur.add_argument("id", type=int)
    qs = add("questions", "questions builders asked")
    qs.add_argument("--all", action="store_true")
    an = add("answer", "answer a builder's question")
    an.add_argument("id")
    g6 = an.add_mutually_exclusive_group(required=True)
    g6.add_argument("--choice", action="append", help="chosen label(s), one --choice per question; "
                    "several labels for one question separated by '|'")
    g6.add_argument("--reject", action="store_true")
    w = add("watch", "print new feed headlines as they arrive; never moves the bookmark (for a monitor)")
    w.add_argument("--consumer")
    w.add_argument("--duration", type=float, default=1800, help="seconds before it exits (default 30 min)")
    w.add_argument("--interval", type=float, default=2.0)
    add("mcp", "run the director's MCP server on stdio (started by Claude Code)")
    hk = add("hook", "Claude Code hook entry points (started by the plugin)")
    hk.add_argument("kind", choices=["session-start", "post-tool-use"])
    pl = add("plugin", "the Claude Code plugin for this installation")
    pl.add_argument("action", choices=["write"])
    pl.add_argument("dir", help="where to write the plugin")
    db = add("dashboard", "open the read-only dashboard (a short-lived token, in this browser tab only)")
    db.add_argument("--no-open", action="store_true")
    add("gate", "are there free resources for heavy work? OK or WAIT with the reason")
    ex = add("export", "write the journal as JSON lines (owner; builder text included, already redacted)")
    ex.add_argument("--out", required=True)
    sa = add("stop-all", "send nothing to any builder until the owner resumes")
    sa.add_argument("--reason")
    ab = add('abort-all', 'pause dispatch and request active-turn and verification cancellation; inspect status')
    ab.add_argument('--reason')
    add("resume-all", "end stop-all (owner)")
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
    ctx = {"home": home, "env": env, "as_owner": args.as_who == "owner", "args": args, "out": out}
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
    if result.pop("_quiet", False):
        return code
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
    err_path = os.path.join(paths.logs(ctx["home"]), "imperiumd.stderr")
    err_f = open(err_path, "ab")  # what the daemon prints before its own log is set up (an import error, ...)
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": err_f, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kw)
    finally:
        err_f.close()
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
            raise Fail(ERROR, f"the daemon exited with code {rc}" + _log_tail(ctx["home"]))
        time.sleep(0.1)
    raise Fail(ERROR, "the daemon did not answer within 15 s" + _log_tail(ctx["home"]))


def _log_tail(home, n=6):
    out = []
    for name in ("imperiumd.stderr", "imperiumd.log"):
        try:
            with open(os.path.join(paths.logs(home), name), encoding="utf-8", errors="replace") as f:
                lines = [x.rstrip() for x in f.readlines()[-n:] if x.strip()]
        except OSError:
            continue
        if lines:
            out.append(f"{name}: " + " | ".join(lines))
    return f"; see {paths.logs(home)}" + ("; " + "; ".join(out) if out else "")


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
    if a.builder_cmd == "token":
        return c.call("POST", "/v1/builders/token", {"name": a.name})
    if a.builder_cmd == "mcp-config" and a.isolated:
        r = c.call("POST", "/v1/builders/token", {"name": a.name, "reveal": True})
        if not r.get("pipe"):
            raise Fail(REFUSED, "isolation mode is off, or no builder accounts are configured ([isolation] in "
                                "imperium.toml)")
        cfg = {"$schema": "https://opencode.ai/config.json",
               "mcp": {"imperium": {"type": "local", "enabled": True,
                                    "command": [sys.executable, "-m", "imperium", "builder-mcp", "--builder", a.name],
                                    "environment": {"IMPERIUM_BUILDER_TOKEN": r["token"], "IMPERIUM_PIPE": r["pipe"]}}}}
        from . import fsutil
        fsutil.atomic_write(os.path.abspath(a.isolated), json.dumps(cfg, indent=2), mode=0o600)
        return {"written": os.path.abspath(a.isolated), "pipe": r["pipe"],
                "note": "give this file to the builder's account only; it holds that builder's token"}
    if a.builder_cmd == "mcp-config":
        b = c.call("GET", "/v1/builders?name=" + urllib.parse.quote(a.name))["builder"]
        cfg = mcp_config(ctx["home"], a.name)
        if not a.write:
            return {"config": cfg, "file": os.path.join(b["directory"], "opencode.json"),
                    "note": "merge this into opencode.json, then restart OpenCode while the builder is idle"}
        path = os.path.join(b["directory"], "opencode.json")
        current = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                current = json.load(f)
        current.setdefault("mcp", {})["imperium"] = cfg["mcp"]["imperium"]
        current.setdefault("$schema", cfg["$schema"])
        from . import fsutil
        fsutil.atomic_write(path, json.dumps(current, indent=2) + "\n")
        return {"written": path, "note": "restart OpenCode while the builder is idle to load the tool"}
    if a.builder_cmd in ("pause", "resume"):
        return c.call("POST", f"/v1/builders/{a.builder_cmd}", {"name": a.name})
    if a.builder_cmd == "allow-version":
        return c.call("POST", "/v1/builders/allow-version", {"name": a.name, "version": a.version})
    if a.acp:
        if a.endpoint or a.session:
            raise Fail(USAGE, "--acp starts its own agent; do not give --endpoint or --session with it")
        return c.call("POST", "/v1/builders", {"adapter": "acp", "name": a.name, "command": a.acp,
                                                "directory": os.path.abspath(a.directory)})
    if not (a.endpoint and a.session):
        raise Fail(USAGE, "give --endpoint and --session (an OpenCode session), or --acp COMMAND")
    body = {"name": a.name, "endpoint": a.endpoint, "session_id": a.session, "directory": a.directory,
            "check": not a.no_check}
    if a.password_env:
        body["password_env"] = a.password_env
    if a.password_file:
        body["password_file"] = os.path.abspath(a.password_file)
    return c.call("POST", "/v1/builders", body)


def cmd_send(ctx):
    a = ctx["args"]
    if a.file:
        with open(a.file, encoding="utf-8") as f:
            text = f.read()
    else:
        text = a.message
    key = a.key or "cli-" + secrets.token_hex(8)
    r = _client(ctx).call("POST", "/v1/send", {"builder": a.builder, "body": text, "client_key": key,
                                               "needs_resources": a.needs_resources})
    r["client_key"] = key
    return r


def cmd_queue(ctx):
    a = ctx["args"]
    q = {}
    if a.builder:
        q["builder"] = a.builder
    if a.all:
        q["all"] = "1"
    return _client(ctx).call("GET", "/v1/queue" + ("?" + urllib.parse.urlencode(q) if q else ""))


def cmd_msg(ctx):
    a = ctx["args"]
    c = _client(ctx)
    if a.msg_cmd == "show":
        return c.call("GET", "/v1/message?" + urllib.parse.urlencode({"id": a.id, "body": "1" if a.body else "0"}))
    if a.msg_cmd == "cancel":
        return c.call("POST", "/v1/cancel", {"id": a.id})
    return c.call("POST", "/v1/message/resolve", {"id": a.id, "choice": a.choice,
                                                  "confirm_may_run_twice": a.confirm_may_run_twice})


def _text_arg(text, path):
    if path:
        with open(path, encoding="utf-8") as f:
            return f.read()
    return text


def cmd_round(ctx):
    a = ctx["args"]
    c = _client(ctx)
    q = urllib.parse.quote
    if a.round_cmd == "open":
        key = a.key or "cli-" + secrets.token_hex(8)
        r = c.call("POST", "/v1/rounds", {"builder": a.builder, "objective": _text_arg(a.objective, a.objective_file),
                                          "client_key": key})
        r["client_key"] = key
        return r
    if a.round_cmd == "list":
        query = {}
        if a.builder:
            query["builder"] = a.builder
        if a.all:
            query["all"] = "1"
        return c.call("GET", "/v1/rounds" + ("?" + urllib.parse.urlencode(query) if query else ""))
    if a.round_cmd == "show":
        return c.call("GET", "/v1/round?id=" + q(a.id))
    if a.round_cmd == "diff":
        return c.call("GET", "/v1/rounds/diff?id=" + q(a.id), timeout=120)
    if a.round_cmd == "message":
        key = a.key or "cli-" + secrets.token_hex(8)
        return c.call("POST", "/v1/rounds/message", {"id": a.id, "body": _text_arg(a.message, a.file),
                                                     "client_key": key})
    if a.round_cmd == "objective":
        return c.call("POST", "/v1/rounds/objective", {"id": a.id, "met": a.verdict == "met", "note": a.note})
    if a.round_cmd == "verify":
        r = c.call("POST", "/v1/rounds/verify", {"id": a.id})
        end = time.monotonic() + a.wait
        while a.wait and time.monotonic() < end:
            cur = c.call("GET", "/v1/round?id=" + q(a.id))
            if not cur["round"]["verify_job"]:
                return cur
            time.sleep(1)
        return r
    body = {"id": a.id, "decision": a.round_cmd, "note": a.note}
    if getattr(a, "override", False):
        body["override"] = True
    return c.call("POST", "/v1/rounds/decide", body, timeout=120)


def cmd_check(ctx):
    a = ctx["args"]
    if a.check_cmd == "list":
        query = {k: v for k, v in (("builder", a.builder), ("round", a.round)) if v}
        return _client(ctx).call("GET", "/v1/checks" + ("?" + urllib.parse.urlencode(query) if query else ""))
    if a.check_cmd == "approve":
        return _owner_or_refuse(ctx).call("POST", "/v1/checks/approve", {"id": a.id}, timeout=60)
    if a.check_cmd == "retire":
        return _owner_or_refuse(ctx).call("POST", "/v1/checks/retire", {"id": a.id})
    body = {"id": a.id, "argv": a.argv, "working_dir": a.dir, "env": a.env, "timeout": a.timeout,
            "must_fail_on_base": a.must_fail_on_base, "depends": a.depends, "required": not a.optional}
    body['base_failure_codes'] = a.base_failure_code or [1]
    body["builder" if a.builder else "round"] = a.builder or a.round
    return _client(ctx).call("POST", "/v1/checks", body, timeout=60)


def cmd_builder_mcp(ctx):
    from . import mcp
    c = Client(ctx["home"], principal="builder:" + ctx["args"].builder, env=ctx["env"])
    mcp.builder_server(c).serve()
    return {"_code": OK, "_quiet": True}


def mcp_config(home, name):
    exe = sys.executable
    return {"$schema": "https://opencode.ai/config.json",
            "mcp": {"imperium": {"type": "local", "enabled": True,
                                 "command": [exe, "-m", "imperium", "--home", home, "builder-mcp", "--builder", name]}}}


def cmd_approvals(ctx):
    a = ctx["args"]
    q = {}
    if a.builder:
        q["builder"] = a.builder
    if a.all:
        q["all"] = "1"
    if a.auto:
        # every unreviewed automatic answer, oldest first; marked reviewed only through the last one shown, and
        # only when nothing was filtered out (S5 review C10)
        c = _owner_or_refuse(ctx)
        rows, after = [], None
        while True:
            page = c.call("GET", "/v1/approvals/auto" + ("" if after is None else f"?after={after}"))
            rows += page["approvals"]
            after = page["through"]
            if not page["more"]:
                break
        if a.builder:
            return {"approvals": [x for x in rows if x["builder"] == a.builder], "marked_reviewed": False,
                    "note": "filtered by builder: nothing was marked reviewed"}
        seen = c.call("POST", "/v1/approvals/report-seen", {"through": after})
        return {"approvals": rows, "marked_reviewed": True, "seen_through": seen["seen_through"]}
    return _client(ctx).call("GET", "/v1/approvals" + ("?" + urllib.parse.urlencode(q) if q else ""))


def cmd_approve(ctx):
    a = ctx["args"]
    return _client(ctx).call("POST", "/v1/approvals/decide", {"id": a.id, "reply": "always" if a.always else "once",
                                                              "note": a.note})


def cmd_deny(ctx):
    a = ctx["args"]
    return _client(ctx).call("POST", "/v1/approvals/decide", {"id": a.id, "reply": "reject", "note": a.note})


def cmd_rule(ctx):
    a = ctx["args"]
    if a.rule_cmd == "list":
        return _client(ctx).call("GET", "/v1/approvals/rules")
    c = _owner_or_refuse(ctx)
    if a.rule_cmd == "remove":
        return c.call("POST", "/v1/approvals/rules/remove", {"id": a.id})
    body = {"permission": a.permission, "pattern": a.pattern, "decision": "allow" if a.allow else "deny"}
    if a.path_under:
        body["path_under"] = os.path.abspath(a.path_under)
    if a.builder:
        body["builder"] = a.builder
    return c.call("POST", "/v1/approvals/rules", body)


def cmd_questions(ctx):
    return _client(ctx).call("GET", "/v1/questions" + ("?all=1" if ctx["args"].all else ""))


def cmd_answer(ctx):
    a = ctx["args"]
    if a.reject:
        return _client(ctx).call("POST", "/v1/questions/answer", {"id": a.id, "reject": True})
    answers = [[x for x in c.split("|") if x] for c in a.choice]
    return _client(ctx).call("POST", "/v1/questions/answer", {"id": a.id, "answers": answers})


def cmd_watch(ctx):
    a = ctx["args"]
    c = _client(ctx)
    consumer = a.consumer or _default_consumer(ctx, c)
    out = ctx["out"]
    end = time.monotonic() + a.duration
    after, warned = None, set()
    while time.monotonic() < end:
        body = {"consumer": consumer, "limit": 100}
        if after is not None:
            body["after"] = after
        try:
            r = c.call("POST", "/v1/events_since", body)
        except DaemonDown as e:
            out.write(f"IMPERIUM_DOWN {e}\n")
            out.flush()
            time.sleep(min(30.0, a.interval * 5))
            continue
        for e in r["events"]:
            out.write(e["headline"] + "\n")
            after = e["seq"]
        for item in (r.get("urgent") or {}).get("items", []):
            if item["seq"] not in warned:
                warned.add(item["seq"])
                out.write("URGENT " + item["headline"] + "\n")
        if after is None:
            after = r.get("high_water") if not r["events"] else after
        out.flush()
        if not r.get("more"):
            time.sleep(a.interval)
    out.write(f"WATCH_EXPIRING resume_after={after}\n")
    out.flush()
    return {"_quiet": True}


def cmd_mcp(ctx):
    from . import director_mcp
    director_mcp.server(Client(ctx["home"], env=ctx["env"])).serve()
    return {"_quiet": True}


def cmd_hook(ctx):
    """Never fails and never blocks Claude Code: every error is swallowed."""
    out = ctx["out"]
    sid = ctx["env"].get("CLAUDE_CODE_SESSION_ID")
    try:
        if ctx["args"].kind == "post-tool-use":
            if not sid:
                return {"_quiet": True}
            mark = os.path.join(ctx["home"], "run", f"presence-{sid}")
            try:
                if time.time() - os.path.getmtime(mark) < 60:
                    return {"_quiet": True}
            except OSError:
                pass
            os.makedirs(os.path.dirname(mark), exist_ok=True)
            with open(mark, "w", encoding="ascii") as f:
                f.write("1")
            Client(ctx["home"], env=ctx["env"]).call("POST", "/v1/director/presence", {}, timeout=3)
            return {"_quiet": True}
        c = Client(ctx["home"], env=ctx["env"])
        if c.health() is None:
            try:
                cmd_up(ctx)
            except Exception:
                pass
        try:
            st = c.call("GET", "/v1/status", timeout=5)
        except tokens.NoCredential:
            out.write("Imperium is available. This session is not its director; to make it one, the owner runs "
                      "`imperium --as owner director claim` in this session.\n")
            return {"_quiet": True}
        lines = [f"Imperium: {st.get('needs_you', 0)} unacknowledged ACTION/CRITICAL event(s) in your feed"]
        if st.get("approvals_open"):
            lines.append(f"{st['approvals_open']} permission ask(s) waiting")
        if st.get("questions_open"):
            lines.append(f"{st['questions_open']} question(s) waiting")
        if st.get("quarantine"):
            lines.append("QUARANTINED: tell the owner")
        if st.get("stop_all"):
            lines.append(f"stop-all is on: {st['stop_all']}")
        out.write("; ".join(lines) + ". Read the feed with events_since before acting (skill: imperium).\n")
    except Exception:
        pass
    return {"_quiet": True}


def cmd_plugin(ctx):
    from . import plugingen
    return plugingen.write(ctx["args"].dir, ctx["home"])


def cmd_dashboard(ctx):
    r = _client(ctx).call("POST", "/v1/dashboard/token", {})
    if not ctx["args"].no_open:
        import webbrowser
        webbrowser.open(r["url"])
    return {"url": r["url"], "note": "read-only; the link works in one tab until the daemon restarts or 12 h idle"}


def cmd_gate(ctx):
    r = _client(ctx).call("GET", "/v1/gate")
    if r.get("gate") != "OK":
        r["_code"] = REFUSED
    return r


def cmd_export(ctx):
    c = _owner_or_refuse(ctx)
    after, n = 0, 0
    tmp = ctx["args"].out + ".partial"
    with open(tmp, "w", encoding="utf-8") as f:
        while True:
            r = c.call("GET", f"/v1/journal?after={after}&limit=1000&full=1", timeout=60)
            for e in r["events"]:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
                after, n = e["seq"], n + 1
            if len(r["events"]) < 1000:
                break
    os.replace(tmp, ctx["args"].out)
    return {"written": ctx["args"].out, "events": n}


def cmd_stop_all(ctx):
    body = {"reason": ctx["args"].reason} if ctx["args"].reason else {}
    return _client(ctx).call("POST", "/v1/stop-all", body)


def cmd_abort_all(ctx):
    return _client(ctx).call('POST', '/v1/abort-all', {'reason': ctx['args'].reason or 'abort requested'})


def cmd_resume_all(ctx):
    return _owner_or_refuse(ctx).call("POST", "/v1/resume-all", {})


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
        cfg = config.load(home)
        check("config", "OK", paths.config(home))
        from . import check_runner
        vc = cfg['verification']
        if vc['backend'] == 'unsafe-local':
            check('verification', 'WARN', 'unsafe-local executes candidate code as your account')
        elif not check_runner.PIN.fullmatch(vc['image']):
            check('verification', 'WARN', 'checks disabled until verification.image is pinned; see docs/VERIFICATION.md')
        elif not shutil.which('docker'):
            check('verification', 'WARN', 'Docker is not installed; checks are disabled, no host fallback')
        else:
            check('verification', 'OK', 'Docker configured; image and runtime are checked before execution')
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
    "send": cmd_send, "queue": cmd_queue, "msg": cmd_msg, "stop-all": cmd_stop_all, "resume-all": cmd_resume_all,
    'abort-all': cmd_abort_all,
    "round": cmd_round, "check": cmd_check, "builder-mcp": cmd_builder_mcp, "approvals": cmd_approvals,
    "approve": cmd_approve, "deny": cmd_deny, "rule": cmd_rule, "questions": cmd_questions, "answer": cmd_answer,
    "watch": cmd_watch, "mcp": cmd_mcp, "hook": cmd_hook, "plugin": cmd_plugin, "dashboard": cmd_dashboard,
    "gate": cmd_gate, "export": cmd_export,
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
