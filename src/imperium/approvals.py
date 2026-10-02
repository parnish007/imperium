"""Approvals and questions (DESIGN §8.3, §8.5, §10.4).

OpenCode's own permission config decides first; only its `ask` cases reach Imperium. An ask is PENDING until
someone decides it. The owner's rules may answer it automatically, but only while the registered director holds a
valid presence lease; a rule match without a lease puts it on HELD, and a held ask is released only by a decision
made by hand. Every automatic answer is journaled with the rule that made it and is listed by
`imperium approvals --auto` (owner, 2026-10-02). Replies are `once` unless the owner chooses `always`.

The reply to OpenCode is an operation: committed with the decision, then sent; until OpenCode confirms it (or the
ask is gone) it is retried every cycle, including after a crash. Questions work the same way, answered by hand only,
with OpenCode's structured answers (one list of chosen labels per question).

Path rules are a tripwire, not a boundary: while a builder can run shell commands it can reach any path, and a
symlink can change between the decision and the use.
"""
import fnmatch
import json
import os
import re

from . import journal

PENDING, HELD, APPROVED, REJECTED, EXPIRED = "PENDING", "HELD", "APPROVED", "REJECTED", "EXPIRED"
ANSWERED = "ANSWERED"
OPEN_STATES = (PENDING, HELD)
REPLIES = ("once", "always", "reject")
SHORT_NAME = re.compile(r"(^|[\\/])[^\\/]*~\d+([\\/.]|$)")  # an 8.3 alias such as PROGRA~1


class ApprovalError(ValueError):
    pass


class Conflict(ApprovalError):
    pass


# --- paths -----------------------------------------------------------------------------------------------

def path_within(candidate, root, case_insensitive=None):
    """True if `candidate` names a location inside `root` (or root itself). Relative candidates are taken
    relative to root. Both are resolved first: symlinks in every existing part of the path (also a parent of a
    file that does not exist yet) and, on Windows, 8.3 short names. Aliases that could dodge the comparison are
    refused (False): UNC and device paths, `~`, and a short name left in a part that does not exist."""
    if not candidate or not root:
        return False
    c = str(candidate)
    if c.startswith("\\\\") or c.startswith("//") or c.startswith("~"):
        return False
    if not os.path.isabs(c):
        c = os.path.join(root, c)
    # non-strict realpath resolves as far as the path exists and appends the rest (S5 review C7)
    c = os.path.realpath(os.path.normpath(os.path.abspath(c)))
    r = os.path.realpath(os.path.normpath(os.path.abspath(root)))
    if SHORT_NAME.search(c):
        return False
    if case_insensitive is None:
        case_insensitive = os.name == "nt"
    if case_insensitive:
        c, r = c.lower(), r.lower()
    return c == r or c.startswith(r.rstrip("\\/") + os.sep)


# --- rules -------------------------------------------------------------------------------------------------

def rules(conn, active_only=True):
    q = "SELECT * FROM approval_rules" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
    return [dict(r) for r in conn.execute(q)]


def add_rule(conn, *, permission, pattern, decision, builder, principal, path_under=None):
    if principal != "owner":
        raise PermissionError("only the owner sets approval rules")
    if decision not in ("allow", "deny"):
        raise ApprovalError("decision must be allow or deny")
    if not permission or not isinstance(permission, str):
        raise ApprovalError("permission is required (e.g. bash, edit, webfetch, or * for any)")
    if path_under is not None and not os.path.isabs(path_under):
        raise ApprovalError("path_under must be an absolute directory")
    cur = conn.execute("INSERT INTO approval_rules(permission, pattern, path_under, decision, builder, created_by, "
                       "created, active) VALUES(?,?,?,?,?,?,?,1)",
                       (permission, pattern or "*", path_under, decision, builder, principal, journal.now()))
    journal.append(conn, "APPROVAL_RULE_ADDED", "NOTICE", caller=principal,
                   data={"rule": cur.lastrowid, "permission": permission, "pattern": pattern or "*",
                         "path_under": path_under, "decision": decision, "builder": builder})
    return cur.lastrowid


def remove_rule(conn, rule_id, principal):
    if principal != "owner":
        raise PermissionError("only the owner changes approval rules")
    if not conn.execute("UPDATE approval_rules SET active=0 WHERE id=? AND active=1", (rule_id,)).rowcount:
        raise ApprovalError(f"no active rule {rule_id}")
    journal.append(conn, "APPROVAL_RULE_REMOVED", "NOTICE", caller=principal, data={"rule": rule_id})


def _rule_matches(rule, a, workspace):
    if rule["builder"] and rule["builder"] != a["builder"]:
        return False
    if not fnmatch.fnmatchcase(a["permission"] or "", rule["permission"]):
        return False
    hits = []
    for p in a["patterns"] or [""]:
        hit = fnmatch.fnmatchcase(p, rule["pattern"])
        if hit and rule["path_under"]:
            hit = path_within(p if os.path.isabs(p) else os.path.join(workspace, p), rule["path_under"])
        hits.append(hit)
    # allow must cover every pattern of the ask; deny fires on any one
    return all(hits) if rule["decision"] == "allow" else any(hits)


MAX_PATTERNS, MAX_PATTERN_LEN = 50, 2000


def maybe_cut(a):
    """True if the stored patterns may be shorter than what the builder asked (they are capped when stored)."""
    pats = a["patterns"] or []
    return len(pats) >= MAX_PATTERNS or any(len(p) >= MAX_PATTERN_LEN for p in pats)


def evaluate(conn, a, workspace):
    """The rule that decides this ask, if any. Deny wins over allow. An ask whose patterns may have been cut is
    never allowed by a rule: what was cut could be what a deny rule would catch (S5 review C6)."""
    matched = [r for r in rules(conn) if _rule_matches(r, a, workspace)]
    deny = [r for r in matched if r["decision"] == "deny"]
    if deny:
        return deny[0]
    if maybe_cut(a):
        return None
    return (matched or [None])[0]


# --- asks --------------------------------------------------------------------------------------------------

FIELDS = ("id", "builder", "permission", "patterns", "always", "state", "decided_by", "by_policy", "rule", "reply",
          "reply_state", "note", "created", "decided_at")


def _row(r):
    if r is None:
        return None
    out = {k: r[k] for k in FIELDS}
    out["patterns"] = json.loads(out["patterns"] or "[]")
    out["always"] = json.loads(out["always"] or "[]")
    out["by_policy"] = bool(out["by_policy"])
    return out


def get(conn, aid):
    r = conn.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
    if r is None:
        raise ApprovalError(f"no approval {aid}")
    return _row(r)


def list_(conn, builder=None, open_only=True, auto_only=False, since_seq=None, limit=200):
    q, args = "SELECT * FROM approvals WHERE 1=1", []
    if builder:
        q += " AND builder=?"
        args.append(builder)
    if open_only:
        q += " AND state IN ('PENDING','HELD')"
    if auto_only:
        q += " AND by_policy=1"
    if since_seq is not None:
        q += " AND decided_seq > ?"
        args.append(since_seq)
    q += " ORDER BY created DESC LIMIT ?"
    args.append(limit)
    return [_row(r) for r in conn.execute(q, args)]


def auto_unreviewed(conn, after_seq, limit=200):
    """Automatic answers not yet reviewed, oldest decision first (the report pages through them)."""
    rows = conn.execute("SELECT * FROM approvals WHERE by_policy=1 AND decided_seq > ? ORDER BY decided_seq LIMIT ?",
                        (after_seq, limit)).fetchall()
    out = []
    for r in rows:
        a = _row(r)
        a["decided_seq"] = r["decided_seq"]
        out.append(a)
    return out


def observe_ask(conn, builder, ask, now):
    """OpenCode listed a new ask (from the reader, in the commit of PERMISSION_ASKED)."""
    if conn.execute("SELECT 1 FROM approvals WHERE id=?", (ask["id"],)).fetchone():
        return None
    conn.execute("INSERT INTO approvals(id, builder, permission, patterns, always, state, by_policy, reply_state, "
                 "created) VALUES(?,?,?,?,?,?,0,'none',?)",
                 (ask["id"], builder, ask.get("permission"),
                  json.dumps([str(p)[:MAX_PATTERN_LEN] for p in ask.get("patterns") or []][:MAX_PATTERNS]),
                  json.dumps([str(p)[:MAX_PATTERN_LEN] for p in ask.get("always") or []][:MAX_PATTERNS]), PENDING,
                  now))
    return get(conn, ask["id"])


def apply_policy(conn, aid, workspace, lease_ok, director, now):
    """Answer a PENDING ask by the owner's rules, or hold it when a rule applies but no lease is valid."""
    a = get(conn, aid)
    if a["state"] != PENDING:
        return a
    rule = evaluate(conn, a, workspace)
    if rule is None:
        return a
    if not lease_ok:
        conn.execute("UPDATE approvals SET state=?, rule=? WHERE id=?", (HELD, rule["id"], aid))
        journal.append(conn, "PERMISSION_HELD", "ACTION", builder=a["builder"],
                       data={"approval": aid, "permission": a["permission"], "rule": rule["id"],
                             "note": "a rule applies but no director is present (no valid lease); decide by hand"})
        return get(conn, aid)
    reply = "once" if rule["decision"] == "allow" else "reject"
    return _decide(conn, a, APPROVED if reply == "once" else REJECTED, reply, f"policy (director {director})",
                   by_policy=True, rule=rule["id"], note=None, now=now)


def decide(conn, aid, reply, principal, note, now):
    """A decision by hand (director or owner). `always` is owner-only. The first committed decision wins."""
    if reply not in REPLIES:
        raise ApprovalError("reply must be once, always or reject")
    if reply == "always" and principal != "owner":
        raise PermissionError("only the owner may answer `always`")
    a = get(conn, aid)
    if a["state"] not in OPEN_STATES:
        raise Conflict(f"ALREADY_DECIDED: approval {aid} is {a['state']} (by {a['decided_by']})")
    return _decide(conn, a, REJECTED if reply == "reject" else APPROVED, reply, principal, by_policy=False,
                   rule=None, note=note, now=now)


def _decide(conn, a, state, reply, who, *, by_policy, rule, note, now):
    seq = journal.append(conn, "PERMISSION_APPROVED" if state == APPROVED else "PERMISSION_REJECTED",
                         "NOTICE" if by_policy else "INFO", builder=a["builder"],
                         caller=None if by_policy else who,
                         data={"approval": a["id"], "permission": a["permission"], "reply": reply,
                               "by_policy": by_policy, "rule": rule, "decided_by": who},
                         untrusted={"patterns": a["patterns"]} if by_policy else None)
    conn.execute("UPDATE approvals SET state=?, reply=?, reply_state='pending', decided_by=?, by_policy=?, rule=?, "
                 "note=?, decided_at=?, decided_seq=? WHERE id=?",
                 (state, reply, who, 1 if by_policy else 0, rule, (note or "")[:2000], now, seq, a["id"]))
    return get(conn, a["id"])


def gone(conn, aid):
    """OpenCode no longer lists the ask: an open one expired; a decided one's reply has landed."""
    r = conn.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
    if r is None:
        return
    a = _row(r)
    if a["state"] in OPEN_STATES:
        conn.execute("UPDATE approvals SET state=?, reply_state='none' WHERE id=?", (EXPIRED, aid))
        journal.append(conn, "PERMISSION_EXPIRED", "NOTICE", builder=a["builder"],
                       data={"approval": aid, "note": "OpenCode no longer lists it (answered elsewhere or the turn "
                                                      "ended)"})
    elif a["reply_state"] in ("pending", "sent"):
        conn.execute("UPDATE approvals SET reply_state='landed' WHERE id=?", (aid,))


def hold_all_pending(conn, principal):
    rows = conn.execute("SELECT id, builder FROM approvals WHERE state='PENDING'").fetchall()
    for aid, builder in rows:
        conn.execute("UPDATE approvals SET state=? WHERE id=?", (HELD, aid))
        journal.append(conn, "PERMISSION_HELD", "ACTION", builder=builder, caller=principal,
                       data={"approval": aid, "note": "stop-all"})
    return len(rows)


def replies_due(conn, builder):
    return [_row(r) for r in conn.execute("SELECT * FROM approvals WHERE builder=? AND reply_state='pending'",
                                          (builder,))]


def reply_sent(conn, aid, ok, detail=None):
    a = get(conn, aid)
    conn.execute("UPDATE approvals SET reply_state=? WHERE id=?", ("sent" if ok else "pending", aid))
    if ok:
        journal.append(conn, "PERMISSION_REPLY_SENT", "INFO", builder=a["builder"],
                       data={"approval": aid, "reply": a["reply"]})


# --- questions ---------------------------------------------------------------------------------------------

def observe_question(conn, builder, q, now):
    if conn.execute("SELECT 1 FROM questions WHERE id=?", (q["id"],)).fetchone():
        return
    shapes = [{"header": str(x.get("header", ""))[:200], "multiple": bool(x.get("multiple")),
               "options": [str(o.get("label", ""))[:200] for o in (x.get("options") or [])][:50]}
              for x in (q.get("questions") or [])][:20]
    conn.execute("INSERT INTO questions(id, builder, shape, state, reply_state, created) VALUES(?,?,?,?, 'none', ?)",
                 (q["id"], builder, json.dumps(shapes), PENDING, now))


def question_get(conn, qid):
    r = conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    if r is None:
        raise ApprovalError(f"no question {qid}")
    out = dict(r)
    out["shape"] = json.loads(out["shape"])
    out["answers"] = json.loads(out["answers"]) if out["answers"] else None
    return out


def questions(conn, builder=None, open_only=True):
    q, args = "SELECT id FROM questions WHERE 1=1", []
    if builder:
        q += " AND builder=?"
        args.append(builder)
    if open_only:
        q += " AND state='PENDING'"
    return [question_get(conn, r[0]) for r in conn.execute(q + " ORDER BY created DESC", args)]


def answer(conn, qid, answers, principal, now, reject=False):
    q = question_get(conn, qid)
    if q["state"] != PENDING:
        raise Conflict(f"ALREADY_DECIDED: question {qid} is {q['state']}")
    if reject:
        answers = None
    else:
        if (not isinstance(answers, list) or len(answers) != len(q["shape"])
                or not all(isinstance(a, list) and all(isinstance(x, str) for x in a) for a in answers)):
            raise ApprovalError(f"answers must be {len(q['shape'])} list(s) of chosen labels, one per question")
        for a, shape in zip(answers, q["shape"]):
            if not shape["multiple"] and len(a) > 1:
                raise ApprovalError(f"question {shape['header']!r} takes one answer")
    conn.execute("UPDATE questions SET state=?, answers=?, decided_by=?, reply_state='pending', decided_at=? "
                 "WHERE id=?", (REJECTED if reject else ANSWERED, json.dumps(answers), principal, now, qid))
    journal.append(conn, "QUESTION_REJECTED" if reject else "QUESTION_ANSWERED", "INFO", builder=q["builder"],
                   caller=principal, data={"question": qid},
                   untrusted={"answers": answers} if answers else None)
    return question_get(conn, qid)


def question_gone(conn, qid):
    r = conn.execute("SELECT state, reply_state, builder FROM questions WHERE id=?", (qid,)).fetchone()
    if r is None:
        return
    if r[0] == PENDING:
        conn.execute("UPDATE questions SET state=? WHERE id=?", (EXPIRED, qid))
        journal.append(conn, "QUESTION_EXPIRED", "NOTICE", builder=r[2], data={"question": qid})
    elif r[1] in ("pending", "sent"):
        conn.execute("UPDATE questions SET reply_state='landed' WHERE id=?", (qid,))


def question_replies_due(conn, builder):
    return [question_get(conn, r[0]) for r in
            conn.execute("SELECT id FROM questions WHERE builder=? AND reply_state='pending'", (builder,))]


def question_reply_sent(conn, qid):
    q = question_get(conn, qid)
    conn.execute("UPDATE questions SET reply_state='sent' WHERE id=?", (qid,))
    journal.append(conn, "QUESTION_REPLY_SENT", "INFO", builder=q["builder"], data={"question": qid})
