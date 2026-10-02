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
    if source_key is not None:
        row = conn.execute("SELECT seq FROM events WHERE source_key=?", (source_key,)).fetchone()
        if row:
            return row[0]
    head_seq, prev_hash = head(conn)
    seq = max(head_seq, _sequence(conn)) + 1
    ts = now()
    data_s = _dump(data if data is not None else {})
    untrusted_s = _dump(untrusted)
    caller_s = caller
    h = _hash(seq, ts, builder, type_, sev, data_s, untrusted_s, source_key, caller_s, prev_hash)
    conn.execute(
        "INSERT INTO events(seq, ts, builder, type, severity, data, untrusted, source_key, caller, prev_hash, hash) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (seq, ts, builder, type_, sev, data_s, untrusted_s, source_key, caller_s, prev_hash, h))
    meta_set(conn, "head_seq", seq)
    meta_set(conn, "head_hash", h)
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


def verify_chain(conn):
    """Recompute every hash from the newest prune boundary to the recorded head."""
    _, expected_prev = boundary(conn)
    checked = 0
    last_seq, last_hash = 0, expected_prev
    for r in conn.execute("SELECT * FROM events ORDER BY seq"):
        if r["prev_hash"] != expected_prev:
            return VerifyResult(False, checked, r["seq"], "link broken (an event is missing or was replaced)")
        h = _hash(r["seq"], r["ts"], r["builder"], r["type"], r["severity"], r["data"], r["untrusted"],
                  r["source_key"], r["caller"], r["prev_hash"])
        if h != r["hash"]:
            return VerifyResult(False, checked, r["seq"], "hash mismatch (the event was edited)")
        expected_prev = r["hash"]
        last_seq, last_hash = r["seq"], r["hash"]
        checked += 1
    head_seq, head_hash = head(conn)
    if (last_seq, last_hash) != (head_seq, head_hash) and not (checked == 0 and head_seq == 0):
        return VerifyResult(False, checked, last_seq + 1, "head mismatch (events after the last one are missing)")
    return VerifyResult(True, checked, None, "ok")
