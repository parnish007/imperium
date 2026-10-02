"""Append-only event journal with an integrity hash chain (DESIGN §5).

The chain detects accidental corruption and naive edits. A process running as the same user can
recompute it, so it is an integrity check, never proof [V-33].
"""
import collections
import datetime
import hashlib
import json

from .store import meta_get, meta_set

SEVERITIES = ("DEBUG", "INFO", "NOTICE", "ACTION", "CRITICAL")
RANK = {name: i for i, name in enumerate(SEVERITIES)}
GENESIS = "0" * 64

VerifyResult = collections.namedtuple("VerifyResult", "ok checked first_bad reason")


class SourceKeyConflict(ValueError):
    """The same idempotency key arrived with different content: an adapter bug, never silently ignored."""


def rank(severity):
    try:
        return RANK[severity]
    except KeyError:
        raise ValueError(f"unknown severity {severity!r}; expected one of {', '.join(SEVERITIES)}") from None


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def _dump(obj):
    return None if obj is None else json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(seq, ts, builder, type_, severity, data, untrusted, source_key, caller, prev_hash):
    body = json.dumps([seq, ts, builder, type_, severity, data, untrusted, source_key, caller, prev_hash],
                      ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def head(conn):
    """(seq, hash) of the newest event, or (0, GENESIS)."""
    seq = meta_get(conn, "head_seq")
    if seq is None:
        return 0, GENESIS
    return int(seq), meta_get(conn, "head_hash")


def append(conn, type_, severity, *, builder=None, data=None, untrusted=None, source_key=None, caller=None):
    """Append one event inside the caller's transaction and return its seq.

    An event whose `source_key` is already journaled is not appended again; the existing seq is returned
    (exactly-once ingestion of adapter observations [R-2]).
    """
    sev = rank(severity)
    data_s = _dump(data if data is not None else {})
    digest = None
    if source_key is not None:
        # Keys live in their own table, which pruning never touches, and are bound to the content [C8].
        digest = hashlib.sha256(json.dumps([type_, builder, data_s]).encode("utf-8")).hexdigest()
        row = conn.execute("SELECT digest, seq FROM source_keys WHERE key=?", (source_key,)).fetchone()
        if row:
            if row[0] == "pre-v3":  # keys migrated from schema 2 had no digest: compare, then bind
                old = conn.execute("SELECT type, builder, data FROM events WHERE seq=?", (row[1],)).fetchone()
                if old is not None and (old[0], old[1], old[2]) != (type_, builder, data_s):
                    raise SourceKeyConflict(f"source key {source_key!r} was journaled as event {row[1]} with "
                                            "different content")
                conn.execute("UPDATE source_keys SET digest=? WHERE key=?", (digest, source_key))
                return row[1]
            if row[0] != digest:
                raise SourceKeyConflict(f"source key {source_key!r} was journaled as event {row[1]} with "
                                        "different content")
            return row[1]
    head_seq, prev_hash = head(conn)
    seq = max(head_seq, _sequence(conn)) + 1
    ts = now()
    untrusted_s = _dump(untrusted)
    caller_s = caller
    h = _hash(seq, ts, builder, type_, sev, data_s, untrusted_s, source_key, caller_s, prev_hash)
    conn.execute(
        "INSERT INTO events(seq, ts, builder, type, severity, data, untrusted, source_key, caller, prev_hash, hash) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (seq, ts, builder, type_, sev, data_s, untrusted_s, source_key, caller_s, prev_hash, h))
    meta_set(conn, "head_seq", seq)
    meta_set(conn, "head_hash", h)
    if source_key is not None:
        conn.execute("INSERT INTO source_keys(key, digest, seq) VALUES(?,?,?)", (source_key, digest, seq))
    return seq


def _sequence(conn):
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
    return row[0] if row else 0


def _row(r):
    return {
        "seq": r["seq"], "ts": r["ts"], "builder": r["builder"], "type": r["type"],
        "severity": SEVERITIES[r["severity"]],
        "data": json.loads(r["data"]),
        "untrusted": json.loads(r["untrusted"]) if r["untrusted"] is not None else None,
        "source_key": r["source_key"], "caller": r["caller"],
        "prev_hash": r["prev_hash"], "hash": r["hash"],
    }


def get(conn, seq):
    r = conn.execute("SELECT * FROM events WHERE seq=?", (seq,)).fetchone()
    return _row(r) if r else None


def raw_row(r):
    """The stored columns of one event, as written to an archive."""
    return {k: r[k] for k in r.keys()}


def headline(ev):
    """One line for feeds and terminals. Never contains builder text (DESIGN §5)."""
    return f"#{ev['seq']} {ev['severity']} {ev['builder'] or '-'} {ev['type']} -> imperium show {ev['seq']}"


def boundary(conn):
    """(seq, hash) of the newest prune boundary, or (0, GENESIS)."""
    r = conn.execute("SELECT boundary_seq, boundary_hash FROM prune_log ORDER BY boundary_seq DESC LIMIT 1").fetchone()
    return (r[0], r[1]) if r else (0, GENESIS)


def _has_anchors(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='anchors'").fetchone() is not None


def anchor(conn):
    """(seq, hash) of the newest owner-accepted break, or (0, GENESIS). Older schemas have none."""
    if not _has_anchors(conn):
        return 0, GENESIS
    r = conn.execute("SELECT seq, hash FROM anchors ORDER BY seq DESC LIMIT 1").fetchone()
    return (r[0], r[1]) if r else (0, GENESIS)


def accepted_breaks(conn):
    if not _has_anchors(conn):
        return []
    return [{"anchored_at_seq": r["seq"], "first_bad": r["first_bad"], "reason": r["reason"], "ts": r["ts"]}
            for r in conn.execute("SELECT * FROM anchors ORDER BY seq")]


def _regions(conn):
    """Owner-accepted broken regions [first_bad, anchored seq + 1], from quarantine releases."""
    if not _has_anchors(conn):
        return []
    return [(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT first_bad, seq, hash FROM anchors WHERE first_bad IS NOT NULL ORDER BY seq")]


def verify_chain(conn):
    """Recompute every hash from the newest prune boundary to the recorded head.

    A failure inside a region the owner accepted (from the recorded first bad event to the anchored head) is
    skipped: verification resumes from the anchor. A failure anywhere else, before or after it, is reported, so
    accepting one break never hides a later edit of older events [S4 review].
    """
    start, expected_prev = boundary(conn)
    regions = _regions(conn)
    checked = 0
    last_seq, last_hash = start, expected_prev
    rows = conn.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq", (start,)).fetchall()
    i = 0
    while i < len(rows):
        r = rows[i]
        bad = None
        if r["prev_hash"] != expected_prev:
            bad = "link broken (an event is missing or was replaced)"
        elif _hash(r["seq"], r["ts"], r["builder"], r["type"], r["severity"], r["data"], r["untrusted"],
                   r["source_key"], r["caller"], r["prev_hash"]) != r["hash"]:
            bad = "hash mismatch (the event was edited)"
        if bad:
            region = next((g for g in regions if g[0] <= r["seq"] <= g[1] + 1), None)
            if region is None:
                return VerifyResult(False, checked, r["seq"], bad)
            regions.remove(region)  # each accepted break is skipped once: the event after it must link to it
            last_seq, last_hash = region[1], region[2]
            expected_prev = last_hash
            while i < len(rows) and rows[i]["seq"] <= last_seq:
                i += 1
            continue
        expected_prev = r["hash"]
        last_seq, last_hash = r["seq"], r["hash"]
        checked += 1
        i += 1
    head_seq, head_hash = head(conn)
    if (last_seq, last_hash) != (head_seq, head_hash) and not (checked == 0 and head_seq == 0):
        return VerifyResult(False, checked, last_seq + 1, "head mismatch (events after the last one are missing)")
    return VerifyResult(True, checked, None, "ok")
