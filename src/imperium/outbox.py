"""The outbox: one row per instruction, one state machine, one reservation per builder (SYSTEM-DESIGN §2-§3).

States and their meaning:
  QUEUED       waiting for the builder to be eligible
  DISPATCHING  committed before the POST; after a crash it becomes UNKNOWN (the POST may or may not have left)
  POSTED       OpenCode answered 2xx; this proves nothing about persistence (prompt_async saves in the background)
  UNKNOWN      the POST failed in transit or with 5xx; reconciled by looking for Imperium's own message id
  UNCERTAIN    not found within the reconcile window; needs a principal's decision; never resent automatically
  DELIVERED    Imperium's own message id is in the builder's history (proof)
  STRANDED     delivered but not run while the builder is idle (#46842); blocks the builder until resolved
  ADMITTED     the builder replied to it (an assistant message whose parent is Imperium's message)
  REJECTED     a definite refusal before anything was saved (not sent)
  CANCELLED    withdrawn by a principal; still watched (a late run is reported)
  SUPERSEDED   replaced by an explicit resend; still watched (if both run: DUPLICATE_RAN)
Outcome classes (E1): not sent = REJECTED, CANCELLED before dispatch; delivered = DELIVERED, ADMITTED;
uncertain = UNCERTAIN; resent = SUPERSEDED.
"""
import hashlib
import secrets
import time

from . import journal

QUEUED, DISPATCHING, POSTED, UNKNOWN, UNCERTAIN = "QUEUED", "DISPATCHING", "POSTED", "UNKNOWN", "UNCERTAIN"
DELIVERED, STRANDED, ADMITTED = "DELIVERED", "STRANDED", "ADMITTED"
REJECTED, CANCELLED, SUPERSEDED = "REJECTED", "CANCELLED", "SUPERSEDED"

ALLOWED = {
    QUEUED: {DISPATCHING, CANCELLED},
    DISPATCHING: {POSTED, UNKNOWN, REJECTED, QUEUED, DELIVERED},
    POSTED: {DELIVERED, UNKNOWN, UNCERTAIN, ADMITTED},
    UNKNOWN: {DELIVERED, UNCERTAIN, ADMITTED},
    UNCERTAIN: {DELIVERED, ADMITTED, SUPERSEDED, CANCELLED},
    DELIVERED: {ADMITTED, STRANDED},
    STRANDED: {ADMITTED, SUPERSEDED, CANCELLED},
    ADMITTED: set(), REJECTED: set(), CANCELLED: set(), SUPERSEDED: set(),
}
IN_FLIGHT = (DISPATCHING, POSTED, UNKNOWN, UNCERTAIN, DELIVERED, STRANDED)
WATCHED_AFTER_END = (CANCELLED, SUPERSEDED)
SOURCE_ORDER = {"owner": 0, "director": 1, "system": 2}
FIELDS = ("id", "builder", "client_key", "source", "principal", "kind", "body", "state", "oc_message_id",
          "supersedes", "created", "dispatched_at", "delivered_at", "admitted_at", "late_admitted_at",
          "decided_by", "note", "round", "round_gen", "needs_resources")


class OutboxError(ValueError):
    pass


class Conflict(OutboxError):
    pass


def new_id():
    return f"{int(time.time() * 1000):011x}{secrets.token_hex(5)}"


def _row(r):
    return None if r is None else {k: r[k] for k in FIELDS}


def get(conn, mid):
    r = conn.execute("SELECT * FROM outbox WHERE id=?", (mid,)).fetchone()
    if r is None:
        raise OutboxError(f"no message {mid}")
    return _row(r)


def by_oc_id(conn, builder, oc_id):
    return _row(conn.execute("SELECT * FROM outbox WHERE builder=? AND oc_message_id=?", (builder, oc_id)).fetchone())


def list_(conn, builder=None, states=None, limit=200):
    q, args = "SELECT * FROM outbox WHERE 1=1", []
    if builder:
        q += " AND builder=?"
        args.append(builder)
    if states:
        q += f" AND state IN ({','.join('?' * len(states))})"
        args += list(states)
    q += " ORDER BY created, rowid"
    if limit is not None:
        q += " LIMIT ?"
        args.append(limit)
    return [_row(r) for r in conn.execute(q, args)]


def enqueue(conn, *, builder, body, client_key, principal, kind="prompt", supersedes=None, round_=None, now=None,
            needs_resources=False):
    """Queue an instruction. The same key with the same body returns the existing row; with another body, refused."""
    if not client_key:
        raise OutboxError("client_key is required (it makes retries of a send safe)")
    source = "owner" if principal == "owner" else ("director" if principal.startswith("director:") else "system")
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    old = conn.execute("SELECT * FROM outbox WHERE builder=? AND client_key=?", (builder, client_key)).fetchone()
    if old is not None:
        if old["body_hash"] != digest:
            raise Conflict(f"client_key {client_key!r} was already used for a different message ({old['id']})")
        return _row(old)
    mid = new_id()
    conn.execute("INSERT INTO outbox(id, builder, client_key, source, principal, kind, body, body_hash, state, "
                 "supersedes, round, needs_resources, created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (mid, builder, client_key, source, principal, kind, body, digest, QUEUED, supersedes, round_,
                  1 if needs_resources else 0, now if now is not None else time.time()))
    data = {"message": mid, "source": source, "kind": kind, "supersedes": supersedes,
            "bytes": len(body.encode("utf-8"))}
    if round_:
        data["round"] = round_
    journal.append(conn, "MSG_QUEUED", "INFO", builder=builder, caller=principal, data=data)
    return get(conn, mid)


def transition(conn, mid, to, *, event, severity="INFO", data=None, caller=None, **fields):
    m = get(conn, mid)
    if to not in ALLOWED[m["state"]]:
        raise OutboxError(f"message {mid} cannot go from {m['state']} to {to}")
    sets = ["state=?"] + [f"{k}=?" for k in fields]
    conn.execute(f"UPDATE outbox SET {', '.join(sets)} WHERE id=?", [to, *fields.values(), mid])
    journal.append(conn, event, severity, builder=m["builder"], caller=caller,
                   data={"message": mid, "from": m["state"], "to": to, **(data or {})})
    if to in (ADMITTED, REJECTED, CANCELLED, SUPERSEDED):
        release(conn, m["builder"], mid)
    if m["round"]:
        from . import rounds  # rounds imports this module
        if to == ADMITTED:
            rounds.on_admitted(conn, get(conn, mid), fields.get("admitted_at") or time.time())
        elif to in (REJECTED, CANCELLED):
            rounds.on_message_ended(conn, m, to, time.time())
    return get(conn, mid)


def requeue(conn, mid, reason):
    """The POST was refused before OpenCode looked at it (auth, rate limit): nothing was saved, so it is queued
    again under a fresh id at the next dispatch."""
    m = transition(conn, mid, QUEUED, event="MSG_REQUEUED", severity="NOTICE", data={"reason": reason},
                   oc_message_id=None, dispatched_at=None)
    release(conn, m["builder"], mid)
    return m


def in_flight(conn, builder):
    return _row(conn.execute(f"SELECT * FROM outbox WHERE builder=? AND state IN ({','.join('?' * len(IN_FLIGHT))}) "
                             "ORDER BY created LIMIT 1", (builder, *IN_FLIGHT)).fetchone())


def recover(conn):
    """At start-up: a message left DISPATCHING may or may not have been posted. It is never posted again;
    it is looked for by its id like any other uncertain send."""
    for m in list_(conn, states=[DISPATCHING], limit=1_000_000):
        transition(conn, m["id"], UNKNOWN, event="MSG_UNKNOWN", severity="NOTICE",
                   data={"reason": "the daemon stopped while sending; looking for it by id"})


# --- reservation: one holder per builder (messages now; Operations later) --------------------------------

def holder(conn, builder):
    r = conn.execute("SELECT holder FROM reservations WHERE builder=?", (builder,)).fetchone()
    return r[0] if r else None


def reserve(conn, builder, who, now):
    h = holder(conn, builder)
    if h is not None and h != who:
        raise Conflict(f"builder {builder} is reserved by {h}")
    conn.execute("INSERT INTO reservations(builder, holder, since) VALUES(?,?,?) "
                 "ON CONFLICT(builder) DO UPDATE SET holder=excluded.holder, since=excluded.since", (builder, who, now))


def release(conn, builder, who):
    conn.execute("DELETE FROM reservations WHERE builder=? AND holder=?", (builder, who))


def next_queued(conn, builder, owner_only=False):
    rows = [m for m in list_(conn, builder, [QUEUED], limit=None) if not owner_only or m["source"] == "owner"]
    rows.sort(key=lambda m: (SOURCE_ORDER.get(m["source"], 9), m["created"]))
    return rows[0] if rows else None


# --- proof from the read path (called inside the reader's batch transaction) -----------------------------

def observed_user(conn, builder, oc_id, token_msg, now):
    """A user message carrying an Imperium token was seen in the builder's history."""
    m = by_oc_id(conn, builder, oc_id)
    if m is not None:
        if m["state"] in (DISPATCHING, POSTED, UNKNOWN):
            transition(conn, m["id"], DELIVERED, event="MSG_DELIVERED", delivered_at=now)
        elif m["state"] == UNCERTAIN:
            transition(conn, m["id"], DELIVERED, event="MSG_FOUND_LATE", severity="NOTICE", delivered_at=now)
        elif m["state"] in WATCHED_AFTER_END:
            journal.append(conn, "LATE_DELIVERY", "NOTICE", builder=builder,
                           data={"message": m["id"], "state": m["state"]}, source_key=f"late-delivery:{m['id']}")
        return
    try:
        claimed = get(conn, token_msg)
    except OutboxError:
        return
    if claimed["builder"] == builder:
        # Imperium's token under another id: a replay copy (e.g. after compaction) or a forgery; never proof.
        journal.append(conn, "REPLAY_SEEN", "NOTICE", builder=builder,
                       data={"message": claimed["id"], "seen_as": oc_id, "own_id": claimed["oc_message_id"]},
                       source_key=f"replay:{oc_id}")


def observed_turn(conn, builder, parent_id, now):
    """The builder started replying to a user message."""
    m = by_oc_id(conn, builder, parent_id)
    if m is None:
        return
    if m["state"] in (DISPATCHING, POSTED, UNKNOWN, UNCERTAIN):
        late = m["state"] == UNCERTAIN
        m = transition(conn, m["id"], DELIVERED, event="MSG_FOUND_LATE" if late else "MSG_DELIVERED",
                       severity="NOTICE" if late else "INFO", delivered_at=now)
    if m["state"] in (DELIVERED, STRANDED):
        transition(conn, m["id"], ADMITTED, event="MSG_ADMITTED", admitted_at=now)
        _check_duplicate(conn, m["id"])
    elif m["state"] in WATCHED_AFTER_END and m["late_admitted_at"] is None:
        conn.execute("UPDATE outbox SET late_admitted_at=? WHERE id=?", (now, m["id"]))
        journal.append(conn, "LATE_ADMISSION", "NOTICE", builder=builder,
                       data={"message": m["id"], "state": m["state"], "note": "it ran after it was withdrawn"})
        _check_duplicate(conn, m["id"])


def _ran(m):
    return m["state"] == ADMITTED or m["late_admitted_at"] is not None


def _check_duplicate(conn, mid):
    m = get(conn, mid)
    pairs = []
    if m["supersedes"]:
        pairs.append((get(conn, m["supersedes"]), m))
    for r in conn.execute("SELECT id FROM outbox WHERE supersedes=?", (mid,)):
        pairs.append((m, get(conn, r[0])))
    for original, replacement in pairs:
        if _ran(original) and _ran(replacement):
            journal.append(conn, "DUPLICATE_RAN", "CRITICAL", builder=m["builder"],
                           data={"original": original["id"], "resent_as": replacement["id"],
                                 "note": "both the original and its explicit resend ran"},
                           source_key=f"duplicate:{original['id']}:{replacement['id']}")


# --- decisions ---------------------------------------------------------------------------------------------

def resolve(conn, mid, choice, principal, confirm_may_run_twice=False, now=None):
    m = get(conn, mid)
    if choice == "wait":
        journal.append(conn, "MSG_DECISION", "INFO", builder=m["builder"], caller=principal,
                       data={"message": mid, "choice": "wait", "state": m["state"]})
        return {"message": m}
    if choice == "cancel":
        if m["state"] == QUEUED or m["state"] in (UNCERTAIN, STRANDED):
            m = transition(conn, mid, CANCELLED, event="MSG_CANCELLED", caller=principal, decided_by=principal,
                           data={"note": "the copy inside the builder, if any, may still run; it stays watched"
                                 if m["state"] != QUEUED else "never sent"})
            return {"message": m}
        raise OutboxError(f"a {m['state']} message cannot be cancelled")
    if choice == "resend":
        if m["state"] not in (UNCERTAIN, STRANDED):
            raise OutboxError(f"only an UNCERTAIN or STRANDED message can be resent (this one is {m['state']})")
        if not confirm_may_run_twice:
            raise OutboxError("resending may make the builder run it twice; confirm with confirm_may_run_twice")
        transition(conn, mid, SUPERSEDED, event="MSG_SUPERSEDED", caller=principal, decided_by=principal)
        new = enqueue(conn, builder=m["builder"], body=m["body"], client_key=f"resend-of-{mid}",
                      principal=principal, kind=m["kind"], supersedes=mid, round_=m["round"], now=now)
        return {"message": get(conn, mid), "resent_as": new["id"]}
    raise OutboxError("choice must be wait, cancel or resend")
