"""Rounds, claims, escalation, trusted checks and decisions (DESIGN §7; SYSTEM-DESIGN §5; S4 review D3-D5).

A round is one objective given to one builder. Claimed, verified and accepted are separate:
  PENDING            the opening message is queued
  OPEN               the builder took the opening message (or a repair message) in
  CLAIMED_READY      the builder says it is done, for the current generation
  CLAIMED_INCOMPLETE the builder says it is not done
  VERIFIED           evidence: every required trusted check passed on a snapshot of this generation, none of the
                     checks' files changed, and the director or owner recorded the objective as met
  ACCEPTED | REJECTED | ABANDONED   a principal's decision (terminal; the first committed decision wins)
A repair message the builder takes in starts a new generation: older claims become stale, VERIFIED is voided.
Escalation is a flag (open/answered), never a state.
"""
import hashlib
import json
import os
import re
import secrets
import time

from . import journal, outbox

PENDING, OPEN, READY, INCOMPLETE, VERIFIED = "PENDING", "OPEN", "CLAIMED_READY", "CLAIMED_INCOMPLETE", "VERIFIED"
ACCEPTED, REJECTED, ABANDONED = "ACCEPTED", "REJECTED", "ABANDONED"
TERMINAL = (ACCEPTED, REJECTED, ABANDONED)
LIVE = (PENDING, OPEN, READY, INCOMPLETE, VERIFIED)
CHECK_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
TEST_PATH = re.compile(r"(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]*$|_test\.[^/]+$|\.(test|spec)\.[^/]+$|"
                       r"(^|/)conftest\.py$|(^|/)(pytest|tox|setup)\.(ini|cfg)$|(^|/)pyproject\.toml$")
REPORT_MIN_INTERVAL = 10.0
ESCALATE_MIN_INTERVAL = 60.0
MAX_LIST = 50
MAX_TEXT = 4000

FIELDS = ("id", "builder", "objective", "nonce", "generation", "state", "escalation", "opened_by", "client_key",
          "opening_msg", "base_commit", "base_tree", "cand_commit", "cand_tree", "cand_generation", "claim_state",
          "claim_generation", "objective_met", "objective_generation", "objective_note", "checks_ok_generation",
          "untrusted", "verify_job", "decided_by", "decision_note", "override", "created", "updated")


class RoundError(ValueError):
    pass


class Conflict(RoundError):
    pass


class TooSoon(RoundError):
    pass


class Rejected(RoundError):
    """A builder's claim refused before it touched the round. The caller journals CLAIM_REJECTED in its own
    transaction (raising here rolls back anything written in this one)."""

    def __init__(self, r, reason):
        super().__init__(reason)
        self.round, self.reason = r, reason


def journal_rejection(conn, r, reason, channel):
    _event(conn, r, "CLAIM_REJECTED", "NOTICE", {"reason": reason[:200], "channel": channel})


def _row(r):
    return None if r is None else {k: r[k] for k in FIELDS}


def get(conn, rid):
    r = conn.execute("SELECT * FROM rounds WHERE id=?", (rid,)).fetchone()
    if r is None:
        raise RoundError(f"no round {rid}")
    return _row(r)


def list_(conn, builder=None, live_only=True, limit=200):
    q, args = "SELECT * FROM rounds WHERE 1=1", []
    if builder:
        q += " AND builder=?"
        args.append(builder)
    if live_only:
        q += f" AND state IN ({','.join('?' * len(LIVE))})"
        args += list(LIVE)
    q += " ORDER BY created DESC, rowid DESC LIMIT ?"
    args.append(limit)
    return [_row(r) for r in conn.execute(q, args)]


def _set(conn, rid, now, **fields):
    fields["updated"] = now
    conn.execute(f"UPDATE rounds SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", [*fields.values(), rid])


def _event(conn, r, type_, severity, data=None, caller=None, untrusted=None):
    journal.append(conn, type_, severity, builder=r["builder"], caller=caller, untrusted=untrusted,
                   data={"round": r["id"], "generation": r["generation"], **(data or {})})


# --- the brief ---------------------------------------------------------------------------------------------

def brief(r, generation=None):
    g = r["generation"] if generation is None else generation
    rid, nonce = r["id"], r["nonce"]
    return f"""Round {rid}, generation {g}. Objective:

{r['objective']}

How to finish this round:
- When the work is done and you have run the relevant tests yourself, report it with the `report_status` tool
  (Imperium MCP server): round="{rid}", nonce="{nonce}", generation={g}, state="ready", the commands you ran in
  `gates`, anything unfinished in `not_done`, open questions in `questions`. If you cannot finish, report
  state="incomplete" and say why. Without the tool, write the same fields as JSON to .imperium/claims/{rid}.json.
- If you are blocked, the task cannot be done as asked, or finishing it would mean weakening, skipping or editing
  tests or checks: do not work around it. Call `escalate` (or write {{"round": "{rid}", "nonce": "{nonce}",
  "escalate": {{...}}}} to the claim file) with the problem, what you tried and what you recommend, then stop and
  wait. Escalating is the expected, correct action in those cases and is never counted against you.
- Do not modify tests, checkers or build scripts to make them pass unless the objective asks for it; such changes
  are detected and flagged. Do not edit .imperium/ except your claim file.
- Your report is checked independently against a snapshot of the code. A claim is evidence, not acceptance.
This brief is also in .imperium/rounds/{rid}.md.
"""


def write_brief(directory, r):
    """Write the brief where the builder can re-read it after its own compaction. Best effort."""
    from . import fsutil
    d = os.path.join(directory, ".imperium", "rounds")
    os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(directory, ".imperium", "claims"), exist_ok=True)
    fsutil.atomic_write(os.path.join(d, f"{r['id']}.md"), brief(r))


def claim_path(directory, rid):
    return os.path.join(directory, ".imperium", "claims", f"{rid}.json")


# --- opening and messages ----------------------------------------------------------------------------------

def open_round(conn, *, builder, objective, client_key, principal, now):
    objective = (objective or "").strip()
    if not objective:
        raise RoundError("an objective is required")
    if len(objective) > 50_000:
        raise RoundError("the objective is longer than 50,000 characters; put details in a file the builder reads")
    if not client_key:
        raise RoundError("client_key is required")
    old = conn.execute("SELECT * FROM rounds WHERE builder=? AND client_key=?", (builder, client_key)).fetchone()
    if old is not None:
        if old["objective"] != objective:
            raise Conflict(f"client_key {client_key!r} was already used for round {old['id']} with another objective")
        return _row(old), False
    rid = "r" + outbox.new_id()
    r = {"id": rid, "builder": builder, "objective": objective, "nonce": secrets.token_hex(8), "generation": 1}
    conn.execute("INSERT INTO rounds(id, builder, objective, nonce, generation, state, opened_by, client_key, "
                 "untrusted, override, created, updated) VALUES(?,?,?,?,?,?,?,?,0,0,?,?)",
                 (rid, builder, objective, r["nonce"], 1, PENDING, principal, client_key, now, now))
    r = get(conn, rid)
    m = outbox.enqueue(conn, builder=builder, body=brief(r), client_key=f"round:{rid}:open", principal=principal,
                       kind="open", round_=rid, now=now)
    _set(conn, rid, now, opening_msg=m["id"])
    _event(conn, r, "ROUND_OPENED", "NOTICE", {"opening_message": m["id"], "objective_bytes": len(objective)},
           caller=principal)
    return get(conn, rid), True


def message(conn, rid, *, body, client_key, principal, now):
    """A repair or continue message within the round. When the builder takes it in, a new generation starts."""
    r = get(conn, rid)
    if r["state"] in TERMINAL:
        raise Conflict(f"round {rid} is {r['state']}")
    body = (body or "").strip()
    if not body:
        raise RoundError("a message body is required")
    return outbox.enqueue(conn, builder=r["builder"], body=body, client_key=f"round:{rid}:{client_key}",
                          principal=principal, kind="repair", round_=rid, now=now)


def header_fields(conn, m):
    """Round fields for the message header, and the generation this message starts (set at dispatch)."""
    if not m.get("round"):
        return "", None
    r = get(conn, m["round"])
    gen = 1 if m["kind"] == "open" else r["generation"] + 1
    return f" round={r['id']} gen={gen}", gen


def body_for(conn, m, gen):
    """The text sent: a repair message carries the claim instructions for its generation."""
    if m.get("kind") != "repair":
        return m["body"]
    r = get(conn, m["round"])
    return (m["body"] + f"\n\n(Round {r['id']} is now at generation {gen}: report with generation={gen}, "
            f"nonce=\"{r['nonce']}\". The full brief is in .imperium/rounds/{r['id']}.md.)")


def on_admitted(conn, m, now):
    """The builder took a round message in (called from the outbox, in the same transaction)."""
    r = get(conn, m["round"])
    if r["state"] in TERMINAL:
        return
    if m["kind"] == "open":
        if r["state"] == PENDING:
            _set(conn, r["id"], now, state=OPEN)
            _event(conn, r, "ROUND_STARTED", "INFO", {"message": m["id"]})
        return
    gen = m.get("round_gen") or r["generation"] + 1
    if gen <= r["generation"]:
        return
    fields = {"generation": gen, "state": OPEN}
    if r["escalation"] == "open":
        fields["escalation"] = "answered"
    _set(conn, r["id"], now, **fields)
    _event(conn, get(conn, r["id"]), "ROUND_REOPENED", "NOTICE",
           {"message": m["id"], "previous_state": r["state"], "voided_verified": r["state"] == VERIFIED,
            "escalation": fields.get("escalation", r["escalation"])})


def on_message_ended(conn, m, to, now):
    """The opening message will never run (rejected, or cancelled before it was sent): the round is abandoned."""
    if m.get("kind") != "open" or not m.get("round"):
        return
    r = get(conn, m["round"])
    if r["state"] != PENDING:
        return
    live = conn.execute("SELECT 1 FROM outbox WHERE round=? AND kind='open' AND id!=? AND state NOT IN "
                        "('ADMITTED','REJECTED','CANCELLED','SUPERSEDED')", (r["id"], m["id"])).fetchone()
    if live:  # a resend of the opening message is on its way
        return
    _set(conn, r["id"], now, state=ABANDONED, decided_by="system",
         decision_note=f"the opening message was {to.lower()}")
    _event(conn, r, "ROUND_ABANDONED", "ACTION", {"reason": f"opening message {m['id']} {to}"})


# --- claims and escalation (from the builder) ---------------------------------------------------------------

def _strings(v, name):
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise RoundError(f"{name} must be a list of strings")
    if len(v) > MAX_LIST:
        raise RoundError(f"{name} has more than {MAX_LIST} items")
    return [x[:MAX_TEXT] for x in v]


def _own_round(conn, rid, builder, nonce):
    r = get(conn, rid)
    if r["builder"] != builder:
        raise RoundError(f"round {rid} does not belong to builder {builder}")
    if not isinstance(nonce, str) or not secrets.compare_digest(nonce, r["nonce"]):
        raise Rejected(r, "wrong nonce for this round (read .imperium/rounds/<round>.md)")
    if r["state"] in TERMINAL:
        raise Rejected(r, f"round {rid} is already {r['state']}")
    return r


def report(conn, *, builder, rid, nonce, generation, state, gates=None, not_done=None, questions=None,
           channel="mcp", now):
    """The builder's claim. Returns a fixed receipt; the claim changes the round only for the current generation."""
    r = _own_round(conn, rid, builder, nonce)
    if state not in ("ready", "incomplete"):
        raise RoundError("state must be 'ready' or 'incomplete'")
    try:
        generation = int(generation)
    except (TypeError, ValueError):
        raise RoundError("generation must be a number") from None
    gates, not_done, questions = _strings(gates, "gates"), _strings(not_done, "not_done"), \
        _strings(questions, "questions")
    if generation != r["generation"]:
        _event(conn, r, "STALE_CLAIM", "NOTICE", {"claimed_generation": generation, "channel": channel})
        return {"recorded": False, "reason": f"stale: the round is at generation {r['generation']}"}
    digest = hashlib.sha256(json.dumps([state, gates, not_done, questions]).encode("utf-8")).hexdigest()
    last = conn.execute("SELECT digest, created FROM claims WHERE round=? AND generation=? ORDER BY id DESC LIMIT 1",
                        (rid, generation)).fetchone()
    if last and last["digest"] == digest:
        return {"recorded": False, "reason": "duplicate of the previous report"}
    if last and channel == "mcp" and now - last["created"] < REPORT_MIN_INTERVAL:
        raise TooSoon(f"at most one report every {REPORT_MIN_INTERVAL:.0f} s per round")
    conn.execute("INSERT INTO claims(round, generation, channel, state, digest, created) VALUES(?,?,?,?,?,?)",
                 (rid, generation, channel, state, digest, now))
    new_state = READY if state == "ready" else INCOMPLETE
    _set(conn, rid, now, state=new_state, claim_state=state, claim_generation=generation)
    _event(conn, r, "CLAIM_READY" if state == "ready" else "CLAIM_INCOMPLETE", "ACTION",
           {"channel": channel, "previous_state": r["state"], "gates": len(gates), "not_done": len(not_done),
            "questions": len(questions)},
           untrusted={"gates": gates, "not_done": not_done, "questions": questions})
    _maybe_verified(conn, rid, now)
    return {"recorded": True, "round": rid, "generation": generation, "state": new_state,
            "note": "recorded as evidence; the round is verified and accepted separately"}


def escalate(conn, *, builder, rid, nonce, issue_type, problem_assessment, approaches_tried=None,
             recommendation=None, channel="mcp", now):
    r = _own_round(conn, rid, builder, nonce)
    last = conn.execute("SELECT MAX(created) FROM claims WHERE round=? AND state='escalate'", (rid,)).fetchone()[0]
    if last is not None and now - last < ESCALATE_MIN_INTERVAL and channel == "mcp":
        raise TooSoon(f"at most one escalation every {ESCALATE_MIN_INTERVAL:.0f} s per round")
    text = {"issue_type": str(issue_type or "")[:200], "problem_assessment": str(problem_assessment or "")[:MAX_TEXT],
            "approaches_tried": _strings(approaches_tried, "approaches_tried"),
            "recommendation": str(recommendation or "")[:MAX_TEXT]}
    if not text["problem_assessment"].strip():
        raise RoundError("problem_assessment is required")
    digest = hashlib.sha256(json.dumps(text, sort_keys=True).encode("utf-8")).hexdigest()
    conn.execute("INSERT INTO claims(round, generation, channel, state, digest, created) VALUES(?,?,?,?,?,?)",
                 (rid, r["generation"], channel, "escalate", digest, now))
    _set(conn, rid, now, escalation="open")
    _event(conn, r, "ESCALATED", "ACTION", {"channel": channel}, untrusted=text)
    return {"recorded": True, "round": rid,
            "note": "escalation received; stop and wait for an answer in this session"}


def apply_claim_file(conn, *, builder, rid, raw, now):
    """The fallback channel: `.imperium/claims/<round>.json`, same validator and state machine."""
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except ValueError as e:
        r = get(conn, rid)
        _event(conn, r, "CLAIM_REJECTED", "NOTICE", {"reason": f"claim file is not valid JSON: {e}"[:200],
                                                     "channel": "file"})
        return None
    if data.get("round", rid) != rid:
        r = get(conn, rid)
        _event(conn, r, "CLAIM_REJECTED", "NOTICE", {"reason": "claim file names another round", "channel": "file"})
        return None
    try:
        if "escalate" in data:
            e = data["escalate"] if isinstance(data["escalate"], dict) else {}
            return escalate(conn, builder=builder, rid=rid, nonce=data.get("nonce"), channel="file", now=now,
                            issue_type=e.get("issue_type"), problem_assessment=e.get("problem_assessment"),
                            approaches_tried=e.get("approaches_tried"), recommendation=e.get("recommendation"))
        return report(conn, builder=builder, rid=rid, nonce=data.get("nonce"), generation=data.get("generation"),
                      state=data.get("state"), gates=data.get("gates"), not_done=data.get("not_done"),
                      questions=data.get("questions"), channel="file", now=now)
    except RoundError as e:
        journal_rejection(conn, get(conn, rid), str(e), "file")
        return None


# --- objective, verification state, decisions --------------------------------------------------------------

def record_objective(conn, rid, met, note, principal, now):
    r = get(conn, rid)
    if r["state"] in TERMINAL:
        raise Conflict(f"round {rid} is {r['state']}")
    _set(conn, rid, now, objective_met=1 if met else 0, objective_generation=r["generation"],
         objective_note=(note or "")[:MAX_TEXT])
    _event(conn, r, "OBJECTIVE_MET" if met else "OBJECTIVE_NOT_MET", "NOTICE", {"note_bytes": len(note or "")},
           caller=principal)
    if not met and r["state"] == VERIFIED:
        _set(conn, rid, now, state=READY)
        _event(conn, r, "VERIFIED_VOIDED", "NOTICE", {"reason": "objective recorded as not met"}, caller=principal)
    _maybe_verified(conn, rid, now)
    return get(conn, rid)


def _maybe_verified(conn, rid, now):
    r = get(conn, rid)
    g = r["generation"]
    ok = (r["state"] == READY and r["claim_generation"] == g and r["cand_generation"] == g
          and r["checks_ok_generation"] == g and not r["untrusted"] and r["objective_met"] == 1
          and r["objective_generation"] == g)
    if ok:
        _set(conn, rid, now, state=VERIFIED)
        _event(conn, r, "ROUND_VERIFIED", "ACTION", {"candidate_tree": r["cand_tree"],
                                                    "note": "evidence complete; a principal may accept"})
    return ok


def decide(conn, rid, decision, principal, note, now, workspace_tree=None, override=False):
    """accept | reject | abandon. The first committed decision wins. Accept needs VERIFIED and an unchanged
    workspace, unless the owner overrides (journaled as such)."""
    r = get(conn, rid)
    if r["state"] in TERMINAL:
        raise Conflict(f"ALREADY_DECIDED: round {rid} is {r['state']} (by {r['decided_by']})")
    if decision not in ("accept", "reject", "abandon"):
        raise RoundError("decision must be accept, reject or abandon")
    if override and principal != "owner":
        raise PermissionError("only the owner can override")
    data = {"note_bytes": len(note or ""), "previous_state": r["state"]}
    if decision == "accept":
        problems = []
        if r["state"] != VERIFIED:
            problems.append(f"the round is {r['state']}, not VERIFIED")
        if r["cand_tree"] and workspace_tree is not None and workspace_tree != r["cand_tree"]:
            problems.append("the workspace changed since the verified snapshot; verify again")
        if problems and not override:
            raise Conflict("cannot accept: " + "; ".join(problems))
        data.update({"candidate_tree": r["cand_tree"], "candidate_commit": r["cand_commit"], "override": bool(problems),
                     "overridden": problems})
        to = ACCEPTED
    else:
        to = REJECTED if decision == "reject" else ABANDONED
    _set(conn, rid, now, state=to, decided_by=principal, decision_note=(note or "")[:MAX_TEXT],
         override=1 if data.get("override") else 0)
    for m in outbox.list_(conn, r["builder"], [outbox.QUEUED]):
        if m.get("round") == rid:
            outbox.transition(conn, m["id"], outbox.CANCELLED, event="MSG_CANCELLED", caller=principal,
                              decided_by=principal, data={"note": f"round {rid} is {to}"})
    _event(conn, r, f"ROUND_{to}", "ACTION" if to == ACCEPTED else "NOTICE", data, caller=principal)
    return get(conn, rid)


# --- trusted checks ----------------------------------------------------------------------------------------

CHECK_FIELDS = ("id", "version", "scope", "argv", "working_dir", "env", "timeout", "must_fail_on_base", "depends",
                "required", "active", "created_by", "created")


def _check_row(r):
    out = {k: r[k] for k in CHECK_FIELDS}
    for k in ("argv", "env", "depends"):
        out[k] = json.loads(out[k])
    out["must_fail_on_base"], out["required"], out["active"] = (bool(out["must_fail_on_base"]),
                                                               bool(out["required"]), bool(out["active"]))
    return out


def check_get(conn, cid):
    r = conn.execute("SELECT * FROM checks WHERE id=? ORDER BY version DESC LIMIT 1", (cid,)).fetchone()
    if r is None:
        raise RoundError(f"no check {cid}")
    return _check_row(r)


def checks_for(conn, builder, rid):
    """The latest active version of each check that applies to this round."""
    rows = conn.execute("SELECT * FROM checks c WHERE version=(SELECT MAX(version) FROM checks WHERE id=c.id) "
                        "AND active=1 AND scope IN (?, ?) ORDER BY id", (f"builder:{builder}", f"round:{rid}"))
    return [_check_row(r) for r in rows]


def check_list(conn, scope=None):
    q = "SELECT * FROM checks c WHERE version=(SELECT MAX(version) FROM checks WHERE id=c.id)"
    args = []
    if scope:
        q += " AND scope=?"
        args.append(scope)
    return [_check_row(r) for r in conn.execute(q + " ORDER BY id", args)]


def define_check(conn, *, cid, scope, argv, working_dir, env, timeout, must_fail_on_base, depends, required,
                 principal):
    """A new check (owner or director), or a new version of one (owner only). `depends` maps each path to the
    hash it had when the check was defined; verification refuses VERIFIED if any of them changed [D5]."""
    if not CHECK_ID.match(cid or ""):
        raise RoundError("a check id is 1-64 characters: lowercase letters, digits, '.', '-', '_'")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        raise RoundError("argv must be a non-empty list of strings (no shell string)")
    if not (0 < float(timeout) <= 6 * 3600):
        raise RoundError("timeout must be between 0 and 6 hours")
    if not isinstance(env, list) or not all(isinstance(e, str) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", e)
                                            for e in env):
        raise RoundError("env must be a list of variable names")
    wd = (working_dir or ".").replace("\\", "/")
    if wd.startswith("/") or ".." in wd.split("/") or re.match(r"^[A-Za-z]:", wd):
        raise RoundError("working_dir must be relative to the builder's directory and stay inside it")
    old = conn.execute("SELECT MAX(version) FROM checks WHERE id=?", (cid,)).fetchone()[0]
    if old is not None and principal != "owner":
        raise PermissionError("only the owner can change an existing check (S4 review D5)")
    version = (old or 0) + 1
    conn.execute("INSERT INTO checks(id, version, scope, argv, working_dir, env, timeout, must_fail_on_base, depends, "
                 "required, active, created_by, created) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)",
                 (cid, version, scope, json.dumps(argv), wd, json.dumps(sorted(set(env))), float(timeout),
                  1 if must_fail_on_base else 0, json.dumps(depends, sort_keys=True), 1 if required else 0,
                  principal, journal.now()))
    journal.append(conn, "CHECK_DEFINED", "NOTICE", caller=principal,
                   data={"check": cid, "version": version, "scope": scope, "argv": argv, "working_dir": wd,
                         "env": sorted(set(env)), "must_fail_on_base": bool(must_fail_on_base),
                         "depends": depends, "required": bool(required)})
    return check_get(conn, cid)


def retire_check(conn, cid, principal):
    c = check_get(conn, cid)
    conn.execute("UPDATE checks SET active=0 WHERE id=?", (cid,))
    journal.append(conn, "CHECK_RETIRED", "NOTICE", caller=principal, data={"check": cid, "version": c["version"]})


def record_run(conn, *, rid, generation, check, target, commit, tree, result, now):
    conn.execute("INSERT INTO check_runs(round, generation, check_id, check_version, target, commit_id, tree, "
                 "exit_code, timed_out, duration, output_sha256, output_path, executable, executable_sha256, "
                 "env_names, created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (rid, generation, check["id"], check["version"], target, commit, tree, result["exit_code"],
                  1 if result["timed_out"] else 0, result["duration"], result["output_sha256"],
                  result["output_path"], result["executable"], result["executable_sha256"],
                  json.dumps(result["env_names"]), now))


def runs(conn, rid):
    return [dict(r) for r in conn.execute("SELECT * FROM check_runs WHERE round=? ORDER BY id", (rid,))]


def claims(conn, rid):
    return [dict(r) for r in conn.execute("SELECT * FROM claims WHERE round=? ORDER BY id", (rid,))]


def is_test_path(path):
    return bool(TEST_PATH.search(path))


def now_ts():
    return time.time()
