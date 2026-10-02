"""imperiumd: the single writer. Local HTTP API on 127.0.0.1 with bearer tokens (DESIGN §3, §11.2, §11.3)."""
import argparse
import datetime
import hashlib
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

from . import (__version__, approvals, audit, backup, builders, config, feeds, journal, liveness, opencode, outbox,
               paths, retention, rounds, snapshot, tokens, verify)
from .engine import Engine
from . import notify, transport
from . import fsutil
from .store import Store, StoreFailed, meta_get, meta_set

log = logging.getLogger("imperiumd")


class AlreadyRunning(RuntimeError):
    pass


_request = threading.local()  # per-request facts for handlers (the transport the call came over)


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
        seen = int(meta_get(conn, "auto_report_seq") or 0)
        auto_unreported = conn.execute("SELECT COUNT(*) FROM approvals WHERE by_policy=1 AND decided_seq > ?",
                                       (seen,)).fetchone()[0]
        approvals_open = conn.execute("SELECT COUNT(*) FROM approvals WHERE state IN ('PENDING','HELD')").fetchone()[0]
        questions_open = conn.execute("SELECT COUNT(*) FROM questions WHERE state='PENDING'").fetchone()[0]
        bl = []
        stop_all = meta_get(conn, "stop_all")
        for b in builders.list_(conn):
            r = conn.execute("SELECT data FROM checkpoints WHERE builder=?", (b["name"],)).fetchone()
            cp = json.loads(r[0]) if r else {}
            queued = conn.execute("SELECT COUNT(*) FROM outbox WHERE builder=? AND state='QUEUED'",
                                  (b["name"],)).fetchone()[0]
            bl.append({"name": b["name"], "endpoint": b["endpoint"], "session_id": b["session_id"],
                       "opencode_version": b["opencode_version"], "allowed_version": b["allowed_version"],
                       "paused": bool(b["paused"]), "status": cp.get("status"),
                       "permissions_pending": cp.get("permissions"), "questions_pending": cp.get("questions"),
                       "busy_subagents": cp.get("busy_children"), "queued": queued,
                       "in_flight": outbox.holder(conn, b["name"]),
                       **(d.engine.state(b["name"]) if d.engine else {})})
    return {"run_id": d.run_id, "pid": d.pid, "port": d.port, "started": d.started, "version": __version__,
            "transport": getattr(_request, "transport", "tcp"), "isolation": bool(d.pipes),
            "notify": {"enabled": bool(d.notifier and d.notifier.enabled),
                       "failing": bool(d.notifier and d.notifier.failures)},
            "principal": principal, "head_seq": head_seq, "chain": d.chain, "archives": d.archives,
            "observe_only": observe_only, "stop_all": stop_all, "quarantine": d.quarantine(), "consumers": consumers,
            "auto_answers_unreported": auto_unreported, "approvals_open": approvals_open,
            "questions_open": questions_open, "director_present": d.lease_ok()[0],
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
    _issue_builder_token(d, name)
    return {"builder": b, "builder_token": f"tokens/builder-{name}"}


def _issue_builder_token(d, name):
    """The token the builder's MCP tool uses. It reaches only the builder routes (report, escalate, rounds)."""
    who = "builder:" + name
    with d.store.tx() as conn:
        tokens.revoke_principal(conn, who)
        raw = tokens.issue(conn, who)
    tokens.write_locator(d.home, who, raw)
    return raw


def r_builder_token(d, principal, body, query):
    """A new builder token. With `reveal` the owner gets the token itself, to put in a builder's own config in
    isolation mode (the builder's account cannot read the owner's runtime folder)."""
    name = str(_req(body, "name"))
    with d.store.read() as conn:
        builders.get(conn, name)
    raw = _issue_builder_token(d, name)
    with d.store.tx() as conn:
        journal.append(conn, "BUILDER_TOKEN_ISSUED", "NOTICE", builder=name, caller=principal,
                       data={"revealed": bool(body.get("reveal"))})
    out = {"builder_token": f"tokens/builder-{name}"}
    if body.get("reveal"):
        out["token"] = raw
        out["pipe"] = d.pipes["builder"].name if "builder" in d.pipes else None
    return out


def r_builder_remove(d, principal, body, query):
    name = str(_req(body, "name"))
    with d.store.tx() as conn:
        builders.remove(conn, name, caller=principal)
        tokens.revoke_principal(conn, "builder:" + name)
    tokens.remove_locator(d.home, "builder:" + name)
    return {"removed": name}


def _active_director(conn):
    return conn.execute("SELECT * FROM directors WHERE released_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()


def r_builder_pause(d, principal, body, query):
    with d.store.tx() as conn:
        return {"builder": builders.set_paused(conn, str(_req(body, "name")), True, principal)}


def r_builder_resume(d, principal, body, query):
    with d.store.tx() as conn:
        return {"builder": builders.set_paused(conn, str(_req(body, "name")), False, principal)}


def r_builder_allow_version(d, principal, body, query):
    with d.store.tx() as conn:
        return {"builder": builders.allow_version(conn, str(_req(body, "name")), _req(body, "version"), principal)}


def r_stop_all(d, principal, body, query):
    """Nothing is sent to any builder until the owner resumes. Anyone may stop; only the owner resumes.
    When the owner stops everything, open asks are held and director tokens are revoked (DESIGN §11.1): the
    director claims again after `resume-all`."""
    reason = str(body.get("reason") or "stopped").strip()[:200]
    with d.store.tx() as conn:
        meta_set(conn, "stop_all", f"{reason} (by {principal})")
        held = revoked = 0
        if principal == "owner":
            held = approvals.hold_all_pending(conn, principal)
            revoked = conn.execute("UPDATE tokens SET revoked=? WHERE principal LIKE 'director:%' AND revoked IS NULL",
                                   (journal.now(),)).rowcount
            conn.execute("UPDATE directors SET released_at=? WHERE released_at IS NULL", (journal.now(),))
        journal.append(conn, "STOP_ALL", "ACTION", caller=principal,
                       data={"reason": reason, "approvals_held": held, "director_tokens_revoked": revoked,
                             "effect": "no message is sent to any builder until `imperium resume-all`"})
    d.leases.clear() if principal == "owner" else None
    return {"stopped": True, "approvals_held": held, "director_tokens_revoked": revoked}


# --- approvals and questions --------------------------------------------------------------------------------

def r_approvals(d, principal, body, query):
    builder = query.get("builder", [None])[0]
    auto = query.get("auto", ["0"])[0] == "1"
    show_all = query.get("all", ["0"])[0] == "1" or auto
    with d.store.read() as conn:
        rows = approvals.list_(conn, builder, open_only=not show_all, auto_only=auto)
    return {"approvals": rows}


def r_approvals_report_seen(d, principal, body, query):
    with d.store.tx() as conn:
        head_seq, _ = journal.head(conn)
        meta_set(conn, "auto_report_seq", head_seq)
    return {"seen_through": head_seq}


def r_approval(d, principal, body, query):
    aid = query.get("id", [None])[0]
    if not aid:
        raise HttpError(400, "id is required")
    with d.store.read() as conn:
        return {"approval": approvals.get(conn, aid)}


def r_approval_decide(d, principal, body, query):
    with d.store.tx() as conn:
        a = approvals.decide(conn, str(_req(body, "id")), str(_req(body, "reply")), principal,
                             str(body.get("note") or ""), d.engine.clock())
    return {"approval": a}


def r_rules(d, principal, body, query):
    with d.store.read() as conn:
        return {"rules": approvals.rules(conn, active_only=query.get("all", ["0"])[0] != "1")}


def r_rule_add(d, principal, body, query):
    with d.store.tx() as conn:
        rid = approvals.add_rule(conn, permission=str(_req(body, "permission")), pattern=body.get("pattern"),
                                 decision=str(_req(body, "decision")), builder=body.get("builder"),
                                 path_under=body.get("path_under"), principal=principal)
    return {"rule": rid}


def r_rule_remove(d, principal, body, query):
    with d.store.tx() as conn:
        approvals.remove_rule(conn, int(_req(body, "id")), principal)
    return {"removed": body["id"]}


def r_questions(d, principal, body, query):
    with d.store.read() as conn:
        return {"questions": approvals.questions(conn, query.get("builder", [None])[0],
                                                 open_only=query.get("all", ["0"])[0] != "1")}


def r_question_answer(d, principal, body, query):
    with d.store.tx() as conn:
        q = approvals.answer(conn, str(_req(body, "id")), body.get("answers"), principal, d.engine.clock(),
                             reject=bool(body.get("reject")))
    return {"question": q}


def r_resume_all(d, principal, body, query):
    with d.store.tx() as conn:
        if not meta_get(conn, "stop_all"):
            raise feeds.Refused("not stopped")
        meta_set(conn, "stop_all", None)
        journal.append(conn, "RESUME_ALL", "NOTICE", caller=principal)
    return {"stopped": False}


def r_send(d, principal, body, query):
    name, text = str(_req(body, "builder")), _req(body, "body")
    if not isinstance(text, str) or not text.strip():
        raise HttpError(400, "body must be non-empty text")
    key = str(_req(body, "client_key")).strip()
    if not key or len(key) > 128:
        raise HttpError(400, "client_key must be 1-128 characters")
    with d.store.tx() as conn:
        builders.get(conn, name)
        m = outbox.enqueue(conn, builder=name, body=text, client_key=key, principal=principal,
                           now=d.engine.clock(), needs_resources=bool(body.get("needs_resources")))
    return {"message": _public(m)}


def _public(m):
    out = dict(m)
    out["body_bytes"] = len(out.pop("body").encode("utf-8"))
    return out


def r_message(d, principal, body, query):
    mid = query.get("id", [None])[0]
    if not mid:
        raise HttpError(400, "id is required")
    with d.store.read() as conn:
        m = outbox.get(conn, mid)
    if query.get("body", ["0"])[0] == "1":
        return {"message": m}
    return {"message": _public(m)}


def r_queue(d, principal, body, query):
    name = query.get("builder", [None])[0]
    show_all = query.get("all", ["0"])[0] == "1"
    states = None if show_all else [outbox.QUEUED, *outbox.IN_FLIGHT]
    with d.store.read() as conn:
        if name:
            builders.get(conn, name)
        return {"messages": [_public(m) for m in outbox.list_(conn, name, states)]}


def r_cancel(d, principal, body, query):
    with d.store.tx() as conn:
        r = outbox.resolve(conn, str(_req(body, "id")), "cancel", principal)
    return {"message": _public(r["message"])}


def r_resolve(d, principal, body, query):
    with d.store.tx() as conn:
        r = outbox.resolve(conn, str(_req(body, "id")), str(_req(body, "choice")), principal,
                           confirm_may_run_twice=bool(body.get("confirm_may_run_twice")), now=d.engine.clock())
    out = {"message": _public(r["message"])}
    if "resent_as" in r:
        out["resent_as"] = r["resent_as"]
    return out


# --- rounds ----------------------------------------------------------------------------------------------

def _public_round(r):
    out = dict(r)
    out.pop("nonce", None)  # the nonce is for the builder; principals see it in the brief if they need it
    out["untrusted"] = bool(out["untrusted"])
    return out


def r_round_open(d, principal, body, query):
    name = str(_req(body, "builder"))
    with d.store.tx() as conn:
        b = builders.get(conn, name)
        r, created = rounds.open_round(conn, builder=name, objective=_req(body, "objective"),
                                       client_key=str(_req(body, "client_key")).strip(), principal=principal,
                                       now=d.engine.clock())
    if created:
        try:
            rounds.write_brief(b["directory"], r)
        except OSError as e:
            with d.store.tx() as conn:
                journal.append(conn, "BRIEF_NOT_WRITTEN", "NOTICE", builder=name,
                               data={"round": r["id"], "error": str(e)[:200]})
    return {"round": _public_round(r), "created": created}


def r_rounds(d, principal, body, query):
    name = query.get("builder", [None])[0]
    live = query.get("all", ["0"])[0] != "1"
    with d.store.read() as conn:
        return {"rounds": [_public_round(r) for r in rounds.list_(conn, name, live_only=live)]}


def r_round(d, principal, body, query):
    rid = query.get("id", [None])[0]
    if not rid:
        raise HttpError(400, "id is required")
    with d.store.read() as conn:
        r = rounds.get(conn, rid)
        msgs = [_public(m) for m in outbox.list_(conn, r["builder"], limit=1_000_000) if m["round"] == rid]
        return {"round": _public_round(r), "messages": msgs, "claims": rounds.claims(conn, rid),
                "check_runs": rounds.runs(conn, rid), "checks": rounds.checks_for(conn, r["builder"], rid)}


def r_round_message(d, principal, body, query):
    with d.store.tx() as conn:
        m = rounds.message(conn, str(_req(body, "id")), body=_req(body, "body"),
                           client_key=str(_req(body, "client_key")), principal=principal, now=d.engine.clock())
    return {"message": _public(m)}


def r_round_verify(d, principal, body, query):
    rid = str(_req(body, "id"))
    with d.store.tx() as conn:
        r = rounds.get(conn, rid)
        if r["state"] in rounds.TERMINAL or r["state"] == rounds.PENDING:
            raise rounds.Conflict(f"round {rid} is {r['state']}; nothing to verify")
        if r["verify_job"]:
            raise rounds.Conflict(f"a verification of {rid} is already running")
        if not r["base_commit"]:
            raise rounds.Conflict(f"round {rid} has no base snapshot (is the workspace a git repository?)")
        job = secrets.token_hex(6)
        conn.execute("UPDATE rounds SET verify_job=? WHERE id=?", (job, rid))
        journal.append(conn, "VERIFY_REQUESTED", "INFO", builder=r["builder"], caller=principal,
                       data={"round": rid, "generation": r["generation"], "job": job})
    d.verifier.submit(rid, job)
    return {"round": rid, "job": job, "note": "running; watch the round's state or the feed"}


def r_round_objective(d, principal, body, query):
    met = _req(body, "met")
    if not isinstance(met, bool):
        raise HttpError(400, "met must be true or false")
    with d.store.tx() as conn:
        r = rounds.record_objective(conn, str(_req(body, "id")), met, str(body.get("note") or ""), principal,
                                    d.engine.clock())
    return {"round": _public_round(r)}


def _workspace_tree(d, builder_name):
    with d.store.read() as conn:
        b = builders.get(conn, builder_name)
    top, _ = snapshot.repo_root(b["directory"])
    return snapshot.tree_of_worktree(top)[0]


def r_round_decide(d, principal, body, query):
    rid, decision = str(_req(body, "id")), str(_req(body, "decision"))
    with d.store.read() as conn:
        r = rounds.get(conn, rid)
    tree = None
    if decision == "accept" and r["cand_tree"]:
        try:
            tree = _workspace_tree(d, r["builder"])
        except snapshot.SnapshotError as e:
            raise HttpError(409, f"cannot compare the workspace with the verified snapshot: {e}") from None
    with d.store.tx() as conn:
        r = rounds.decide(conn, rid, decision, principal, str(body.get("note") or ""), d.engine.clock(),
                          workspace_tree=tree, override=bool(body.get("override")))
    return {"round": _public_round(r)}


def r_round_diff(d, principal, body, query):
    rid = query.get("id", [None])[0]
    if not rid:
        raise HttpError(400, "id is required")
    with d.store.read() as conn:
        r = rounds.get(conn, rid)
        b = builders.get(conn, r["builder"])
    if not r["base_commit"]:
        raise HttpError(409, f"round {rid} has no base snapshot")
    top, _ = snapshot.repo_root(b["directory"])
    problems = []
    for kind, commit, tree in (("base", r["base_commit"], r["base_tree"]),
                               ("candidate", r["cand_commit"], r["cand_tree"])):
        if commit and snapshot._git(["rev-parse", f"{commit}^{{tree}}"], top, check=False) != tree:
            problems.append(kind)
    if problems:
        with d.store.tx() as conn:
            journal.append(conn, "SNAPSHOT_MISMATCH", "CRITICAL", builder=r["builder"],
                           data={"round": rid, "snapshots": problems})
    target, against = (r["cand_commit"], "candidate") if r["cand_commit"] else \
        (snapshot.tree_of_worktree(top)[0], "workspace now")
    changed = snapshot.changed_files(top, r["base_commit"], target)
    os.makedirs(os.path.join(d.home, "diffs"), exist_ok=True)
    path = os.path.join(d.home, "diffs", f"{rid}-g{r['generation']}.diff")
    with open(path, "w", encoding="utf-8") as f:
        f.write(snapshot.diff(top, r["base_commit"], target))
    return {"round": rid, "against": against, "files": [{"status": s_, "path": p_, "test": rounds.is_test_path(p_)}
                                                        for s_, p_ in changed],
            "stat": snapshot.diff_stat(top, r["base_commit"], target), "diff_path": path,
            "snapshot_mismatch": problems}


# --- trusted checks ------------------------------------------------------------------------------------------

def _scope_and_dir(conn, body):
    if body.get("round"):
        r = rounds.get(conn, str(body["round"]))
        return f"round:{r['id']}", builders.get(conn, r["builder"])["directory"]
    name = str(_req(body, "builder"))
    return f"builder:{name}", builders.get(conn, name)["directory"]


def r_check_define(d, principal, body, query):
    with d.store.read() as conn:
        scope, directory = _scope_and_dir(conn, body)
    paths_ = body.get("depends") or []
    if not isinstance(paths_, list) or not all(isinstance(x, str) for x in paths_):
        raise HttpError(400, "depends must be a list of paths")
    depends = verify.hash_depends(directory, paths_)
    with d.store.tx() as conn:
        c = rounds.define_check(conn, cid=str(_req(body, "id")), scope=scope, argv=_req(body, "argv"),
                                working_dir=body.get("working_dir") or ".", env=body.get("env") or [],
                                timeout=body.get("timeout", 1800), must_fail_on_base=bool(body.get("must_fail_on_base")),
                                depends=depends, required=body.get("required", True) is not False, principal=principal)
    return {"check": c}


def r_check_approve(d, principal, body, query):
    """The owner accepts the current versions of a check's files (after reading the round's diff)."""
    cid = str(_req(body, "id"))
    with d.store.read() as conn:
        c = rounds.check_get(conn, cid)
        kind, ref = c["scope"].split(":", 1)
        directory = builders.get(conn, ref if kind == "builder" else rounds.get(conn, ref)["builder"])["directory"]
    top, prefix = snapshot.repo_root(directory)
    rel = []
    for path in c["depends"]:
        if os.path.isabs(path):
            rel.append(path)
        else:
            rel.append(os.path.relpath(os.path.join(top, *path.split("/")), directory).replace("\\", "/"))
    depends = verify.hash_depends(directory, rel)
    with d.store.tx() as conn:
        n = rounds.define_check(conn, cid=cid, scope=c["scope"], argv=c["argv"], working_dir=c["working_dir"],
                                env=c["env"], timeout=c["timeout"], must_fail_on_base=c["must_fail_on_base"],
                                depends=depends, required=c["required"], principal=principal)
        journal.append(conn, "CHECK_REAPPROVED", "NOTICE", caller=principal,
                       data={"check": cid, "version": n["version"], "previous": c["depends"], "now": depends})
    return {"check": n}


def r_check_retire(d, principal, body, query):
    with d.store.tx() as conn:
        rounds.retire_check(conn, str(_req(body, "id")), principal)
    return {"retired": body["id"]}


def r_checks(d, principal, body, query):
    scope = None
    if query.get("builder"):
        scope = "builder:" + query["builder"][0]
    elif query.get("round"):
        scope = "round:" + query["round"][0]
    with d.store.read() as conn:
        return {"checks": rounds.check_list(conn, scope)}


# --- the builder's own routes (builder token only) --------------------------------------------------------

def _builder_of(principal):
    return principal.split(":", 1)[1]


def r_builder_rounds(d, principal, body, query):
    with d.store.read() as conn:
        rs = rounds.list_(conn, _builder_of(principal))
    return {"rounds": [{"round": r["id"], "objective": r["objective"], "nonce": r["nonce"],
                        "generation": r["generation"], "state": r["state"], "escalation": r["escalation"]}
                       for r in rs if r["state"] != rounds.PENDING]}


def _builder_call(d, principal, fn, **kw):
    try:
        with d.store.tx() as conn:
            return fn(conn, builder=_builder_of(principal), now=d.engine.clock(), **kw)
    except rounds.Rejected as e:
        with d.store.tx() as conn:
            rounds.journal_rejection(conn, e.round, e.reason, "mcp")
        raise HttpError(400, e.reason) from None


def r_builder_report(d, principal, body, query):
    return _builder_call(d, principal, rounds.report, rid=str(_req(body, "round")), nonce=body.get("nonce"),
                         generation=body.get("generation"), state=body.get("state"), gates=body.get("gates"),
                         not_done=body.get("not_done"), questions=body.get("questions"))


def r_builder_escalate(d, principal, body, query):
    return _builder_call(d, principal, rounds.escalate, rid=str(_req(body, "round")), nonce=body.get("nonce"),
                         issue_type=body.get("issue_type"), problem_assessment=body.get("problem_assessment"),
                         approaches_tried=body.get("approaches_tried"), recommendation=body.get("recommendation"))


def r_presence(d, principal, body, query):
    """The director's PostToolUse hook: it is at work in its session (renews the presence lease)."""
    return {"present": principal.startswith("director:")}


def r_gate(d, principal, body, query):
    verdict, reason, m = liveness.gate(d.cfg["resources"]["min_free_gb"])
    return {"gate": verdict, "reason": reason, **m}


def r_journal(d, principal, body, query):
    """Read the journal without touching any feed (dashboard, export). Builder text only for the owner's export."""
    after = int(query.get("after", ["0"])[0] or 0)
    limit = max(1, min(int(query.get("limit", ["100"])[0] or 100), 1000))
    full = query.get("full", ["0"])[0] == "1"
    if full and principal != "owner":
        raise HttpError(403, "the full export is the owner's")
    with d.store.read() as conn:
        if after:
            rows = conn.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (after, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM (SELECT * FROM events ORDER BY seq DESC LIMIT ?) ORDER BY seq",
                                (limit,)).fetchall()
    out = []
    for r in rows:
        e = journal._row(r)
        e["headline"] = journal.headline(e)
        if not full:
            e.pop("untrusted", None)
        out.append(e)
    return {"events": out}


def r_dashboard_token(d, principal, body, query):
    raw = secrets.token_urlsafe(32)
    d.dash_tokens[hashlib.sha256(raw.encode()).hexdigest()] = time.monotonic()
    with d.store.tx() as conn:
        journal.append(conn, "DASHBOARD_TOKEN", "INFO", caller=principal, data={"read_only": True})
    return {"token": raw, "url": f"http://127.0.0.1:{d.port}/dashboard#token={raw}"}


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
    ("POST", "/v1/builders/pause"): (r_builder_pause, "owner", True),
    ("POST", "/v1/builders/resume"): (r_builder_resume, "owner", True),
    ("POST", "/v1/builders/allow-version"): (r_builder_allow_version, "owner", True),
    ("POST", "/v1/stop-all"): (r_stop_all, "any", True),
    ("POST", "/v1/resume-all"): (r_resume_all, "owner", True),
    ("POST", "/v1/send"): (r_send, "any", True),
    ("GET", "/v1/message"): (r_message, "any", False),
    ("GET", "/v1/queue"): (r_queue, "any", False),
    ("POST", "/v1/cancel"): (r_cancel, "any", True),
    ("POST", "/v1/message/resolve"): (r_resolve, "any", True),
    ("POST", "/v1/builders/token"): (r_builder_token, "owner", True),
    ("POST", "/v1/rounds"): (r_round_open, "any", True),
    ("GET", "/v1/rounds"): (r_rounds, "any", False),
    ("GET", "/v1/round"): (r_round, "any", False),
    ("POST", "/v1/rounds/message"): (r_round_message, "any", True),
    ("POST", "/v1/rounds/verify"): (r_round_verify, "any", True),
    ("POST", "/v1/rounds/objective"): (r_round_objective, "any", True),
    ("POST", "/v1/rounds/decide"): (r_round_decide, "any", True),
    ("GET", "/v1/rounds/diff"): (r_round_diff, "any", False),
    ("POST", "/v1/checks"): (r_check_define, "any", True),
    ("POST", "/v1/checks/approve"): (r_check_approve, "owner", True),
    ("POST", "/v1/checks/retire"): (r_check_retire, "owner", True),
    ("GET", "/v1/checks"): (r_checks, "any", False),
    ("GET", "/v1/approvals"): (r_approvals, "any", False),
    ("POST", "/v1/approvals/report-seen"): (r_approvals_report_seen, "owner", True),
    ("GET", "/v1/approval"): (r_approval, "any", False),
    ("POST", "/v1/approvals/decide"): (r_approval_decide, "any", True),
    ("GET", "/v1/approvals/rules"): (r_rules, "any", False),
    ("POST", "/v1/approvals/rules"): (r_rule_add, "owner", True),
    ("POST", "/v1/approvals/rules/remove"): (r_rule_remove, "owner", True),
    ("GET", "/v1/questions"): (r_questions, "any", False),
    ("POST", "/v1/questions/answer"): (r_question_answer, "any", True),
    ("POST", "/v1/director/presence"): (r_presence, "any", False),
    ("GET", "/v1/gate"): (r_gate, "any", False),
    ("GET", "/v1/journal"): (r_journal, "any", False),
    ("POST", "/v1/dashboard/token"): (r_dashboard_token, "any", False),
    ("GET", "/v1/builder/rounds"): (r_builder_rounds, "builder", False),
    ("POST", "/v1/builder/report"): (r_builder_report, "builder", True),
    ("POST", "/v1/builder/escalate"): (r_builder_escalate, "builder", True),
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


# Calls by which the registered director shows it is present (DESIGN §8.3): reading what it was shown, or deciding.
# Status, liveness and watch do not count; there is no heartbeat call.
RENEWS_LEASE = {"/v1/events_since", "/v1/ack", "/v1/show", "/v1/send", "/v1/cancel", "/v1/message/resolve",
                "/v1/director/presence",
                "/v1/approvals/decide", "/v1/questions/answer", "/v1/rounds", "/v1/rounds/message",
                "/v1/rounds/verify", "/v1/rounds/objective", "/v1/rounds/decide"}


# State changes still allowed while quarantined: reading feeds (which records what was shown), backups,
# stopping, and the owner's release. Everything that dispatches, decides or changes trust waits [C6].
QUARANTINE_OK = {"/v1/events_since", "/v1/ack", "/v1/consumers", "/v1/backup", "/v1/quarantine/release",
                 "/v1/shutdown", "/v1/stop-all", "/v1/cancel"}


def _req(body, key):
    if key not in body:
        raise HttpError(400, f"{key} is required")
    return body[key]


def _opt_int(body, key):
    v = body.get(key)
    return None if v is None else int(v)


DASHBOARD_FILES = {"/dashboard": "index.html", "/dashboard/": "index.html", "/dashboard/app.js": "app.js",
                   "/dashboard/app.css": "app.css"}
DASHBOARD_IDLE = 12 * 3600


class _Static(Exception):
    def __init__(self, path):
        super().__init__(path)
        self.path = path


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

    def _check_channel(self, d, principal):
        """Isolation mode: each token only on its channel; over TCP only the read-only dashboard."""
        addr = self.client_address
        on_pipe = isinstance(addr, tuple) and len(addr) == 3 and addr[0] == "pipe"
        _request.transport = f"pipe:{addr[1]}" if on_pipe else "tcp"
        if not d.pipes:
            return
        if not on_pipe:
            if principal != "dashboard":
                raise HttpError(403, "isolation mode: this token is accepted only over Imperium's pipe")
            return
        want = "builder" if principal.startswith("builder:") else "owner"
        if addr[1] != want or principal == "dashboard":
            raise HttpError(403, f"a {principal.split(':')[0]} token is accepted only on the {want} pipe")

    def _static(self, path):
        name = DASHBOARD_FILES[path]
        try:
            with open(os.path.join(os.path.dirname(__file__), "dashboard", name), "rb") as f:
                data = f.read()
        except OSError:
            data, name = b"not found", "x.txt"
        ctype = {"html": "text/html", "js": "text/javascript", "css": "text/css"}.get(name.rsplit(".", 1)[-1],
                                                                                    "text/plain")
        allowed = {f"127.0.0.1:{self.server.imperium.port}", f"localhost:{self.server.imperium.port}"}
        if (self.headers.get("Host") or "").lower() not in allowed:
            data, ctype = b"bad Host header", "text/plain"
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
                         "form-action 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, method):
        d = self.server.imperium
        principal = None
        route = f"{method} {urllib.parse.urlsplit(self.path).path}"
        result = "ok"
        try:
            status, payload, principal = self._handle(d, method)
        except _Static as s:
            return self._static(s.path)
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
        on_pipe = isinstance(self.client_address, tuple) and self.client_address[:1] == ("pipe",)
        if not on_pipe:  # Host and Origin defend the TCP port against web pages; a pipe is not reachable by them
            if (self.headers.get("Host") or "").lower() not in allowed:
                raise HttpError(403, "bad Host header")
            origin = self.headers.get("Origin")
            if origin is not None and origin.lower() not in {f"http://{a}" for a in allowed}:
                raise HttpError(403, "foreign Origin")
        url = urllib.parse.urlsplit(self.path)
        if method == "GET" and url.path in DASHBOARD_FILES:
            raise _Static(url.path)
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
            if principal is None and raw and d.dashboard_ok(raw):
                if method != "GET" or who != "any":
                    raise HttpError(403, "the dashboard token is read-only")
                principal = "dashboard"
            if principal is None:
                raise HttpError(401, "missing or invalid bearer token")
            self._check_channel(d, principal)
            if who == "owner" and principal != "owner":
                raise HttpError(403, "owner only")
            is_builder = principal.startswith("builder:")
            if who == "builder" and not is_builder:
                raise HttpError(403, "builder tool only")
            if who != "builder" and is_builder:
                raise HttpError(403, "a builder token reaches only the builder tool's routes")
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
        except (builders.Conflict, outbox.Conflict, rounds.Conflict, approvals.Conflict, snapshot.SnapshotError) as e:
            raise HttpError(409, str(e)) from None
        except rounds.TooSoon as e:
            raise HttpError(429, str(e)) from None
        except (feeds.Refused, retention.PruneRefused) as e:
            raise HttpError(409, str(e)) from None
        except PermissionError as e:
            raise HttpError(403, str(e)) from None
        except verify.VerifyError as e:
            raise HttpError(400, str(e)) from None
        except StoreFailed as e:
            raise HttpError(503, str(e)) from None
        except (feeds.FeedError, ValueError, TypeError) as e:
            raise HttpError(400, str(e)) from None
        out = dict(out)
        out["ok"] = True
        if url.path in RENEWS_LEASE and principal and principal.startswith("director:"):
            d.renew_lease(principal)
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
        self.verifier = None
        self.notifier = None
        self.pipes = {}
        self.leases = {}  # director principal -> monotonic time of its last presence call (this run only)
        self.dash_tokens = {}  # sha256 of a read-only dashboard token -> last use (this run only)
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
            self._start_pipes()
            self._write_info()
            self.engine = Engine(self)
            self.verifier = verify.Verifier(self)
            self.verifier.start()
            self.engine.start()
            self.notifier = notify.Notifier(self)
            self.notifier.start()
        except BaseException:
            for p in self.pipes.values():
                p.stop()
            if self.store is not None:
                self.store.close()
            release_lock(self._lock)
            raise
        log.info("imperiumd %s listening on 127.0.0.1:%s (run %s)", __version__, self.port, self.run_id)
        return self

    def dashboard_ok(self, raw):
        key = hashlib.sha256(raw.encode()).hexdigest()
        t = self.dash_tokens.get(key)
        if t is None or time.monotonic() - t > DASHBOARD_IDLE:
            self.dash_tokens.pop(key, None)
            return False
        self.dash_tokens[key] = time.monotonic()
        return True

    def renew_lease(self, principal):
        with self.store.read() as conn:
            r = conn.execute("SELECT session_id FROM directors WHERE released_at IS NULL ORDER BY id DESC LIMIT 1")\
                .fetchone()
        if r is not None and principal == "director:" + r[0]:
            self.leases[principal] = time.monotonic()

    def lease_ok(self):
        """(valid, director principal): the registered director called within the lease time, in this run."""
        with self.store.read() as conn:
            r = conn.execute("SELECT session_id FROM directors WHERE released_at IS NULL ORDER BY id DESC LIMIT 1")\
                .fetchone()
        if r is None:
            return False, None
        who = "director:" + r[0]
        t = self.leases.get(who)
        return (t is not None and time.monotonic() - t < self.cfg["approvals"]["lease_ttl"]), who

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
            outbox.recover(conn)
            for r in conn.execute("SELECT id, builder, generation FROM rounds WHERE verify_job IS NOT NULL").fetchall():
                journal.append(conn, "VERIFY_INTERRUPTED", "NOTICE", builder=r[1],
                               data={"round": r[0], "generation": r[2], "note": "the daemon stopped; verify again"})
            conn.execute("UPDATE rounds SET verify_job=NULL WHERE verify_job IS NOT NULL")
            conn.execute("DELETE FROM reservations WHERE holder LIKE 'verify:%'")
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

    def _start_pipes(self):
        iso = self.cfg["isolation"]
        if not iso["owner_accounts"]:
            return
        for channel, allowed in (("owner", iso["owner_accounts"]), ("builder", iso["builder_accounts"])):
            if not allowed:
                continue
            srv = transport.make_server(self.home, channel, [str(x) for x in allowed], _Handler, self.server)
            srv.start()
            self.pipes[channel] = srv

    def _write_info(self):
        info = {"port": self.port, "pid": self.pid, "run_id": self.run_id, "started": self.started,
                "version": __version__}
        if self.pipes:
            info["pipes"] = {c: s.name for c, s in self.pipes.items()}
        fsutil.atomic_write(paths.daemon_json(self.home), json.dumps(info), mode=0o600)

    def serve_forever(self):
        self.server.serve_forever(poll_interval=0.2)

    def shutdown(self):
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        for p in self.pipes.values():
            p.stop()
        self.server.shutdown()
        self.server.server_close()
        if self.engine:
            self.engine.stop()
        if self.verifier:
            self.verifier.stop()
        if self.notifier:
            self.notifier.stop()
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
