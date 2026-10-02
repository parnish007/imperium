"""Call audit: one row per API call, in a separate capped table, never in the journal (DESIGN §11.2, P2-13)."""
from . import journal
from .store import meta_get, meta_set

DEFAULT_CAP = 100_000


def record(conn, principal, peer_pid, route, result, cap=DEFAULT_CAP):
    cur = conn.execute("INSERT INTO call_audit(ts, principal, peer_pid, route, result) VALUES(?,?,?,?,?)",
                       (journal.now(), principal, peer_pid, route, result))
    new_id = cur.lastrowid
    if new_id > cap:
        gone = conn.execute("DELETE FROM call_audit WHERE id <= ?", (new_id - cap,)).rowcount
        if gone:
            meta_set(conn, "audit_dropped", dropped(conn) + gone)


def dropped(conn):
    return int(meta_get(conn, "audit_dropped") or 0)
