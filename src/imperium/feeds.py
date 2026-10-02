"""Feeds: one bookmark per reader with a fixed severity floor (DESIGN §8.1).

Guarantee: every event at or above a feed's floor is shown to that feed at least once, in order,
and is never marked read without having been shown. `shown_through` is the highest seq up to which
every feed event has been returned to the consumer; `ack` beyond it is refused.
"""
from . import journal

URGENT_ITEMS = 5


class FeedError(ValueError):
    pass


class Refused(PermissionError):
    """The action is not allowed in the feed's current state."""


class NotYours(Refused):
    """The consumer belongs to another principal."""


def create(conn, name, *, principal, floor):
    """Create a consumer. Re-creating it identically is a no-op; a different floor or principal is refused."""
    fl = journal.rank(floor)
    row = conn.execute("SELECT principal, floor FROM consumers WHERE name=?", (name,)).fetchone()
    if row:
        if (row["principal"], row["floor"]) != (principal, fl):
            raise FeedError(f"consumer {name!r} exists with another principal or floor; a floor is fixed at "
                            "creation, so create a new consumer instead")
        return
    conn.execute("INSERT INTO consumers(name, principal, floor, acked_seq, shown_through, created) "
                 "VALUES(?,?,?,?,?,?)", (name, principal, fl, 0, 0, journal.now()))


def get(conn, name):
    r = conn.execute("SELECT * FROM consumers WHERE name=?", (name,)).fetchone()
    if not r:
        raise FeedError(f"no consumer named {name!r}")
    return {"name": r["name"], "principal": r["principal"], "floor": journal.SEVERITIES[r["floor"]],
            "acked_seq": r["acked_seq"], "shown_through": r["shown_through"]}


def _own(conn, name, principal):
    c = get(conn, name)
    if c["principal"] != principal:
        raise NotYours(f"consumer {name!r} belongs to another principal")
    return c


def events_since(conn, name, principal, *, limit=50, after=None, high_water=None):
    """Return the next page of the feed in seq order. Must run inside a write transaction.

    The first page of a drain (no `high_water` given) captures the current head as `high_water`;
    later pages pass it back, so a drain always ends even while new events keep arriving [P2-22].
    """
    if limit < 1 or limit > 500:
        raise FeedError("limit must be between 1 and 500")
    c = _own(conn, name, principal)
    floor = journal.rank(c["floor"])
    if high_water is None:
        high_water = journal.head(conn)[0]
    start = c["acked_seq"] if after is None else max(int(after), 0)
    rows = conn.execute(
        "SELECT * FROM events WHERE seq > ? AND seq <= ? AND severity >= ? ORDER BY seq LIMIT ?",
        (start, high_water, floor, limit)).fetchall()
    events = [journal._row(r) for r in rows]
    for e in events:
        e["headline"] = journal.headline(e)
    end = events[-1]["seq"] if len(events) == limit else high_water
    if start <= c["shown_through"] and end > c["shown_through"]:
        conn.execute("UPDATE consumers SET shown_through=? WHERE name=?", (end, name))
    return {"events": events, "high_water": high_water, "more": len(events) == limit,
            "urgent": urgent(conn, name)}


def urgent(conn, name):
    c = get(conn, name)
    rows = conn.execute("SELECT * FROM events WHERE seq > ? AND severity = ? ORDER BY seq",
                        (c["acked_seq"], journal.RANK["CRITICAL"])).fetchall()
    items = [{"seq": r["seq"], "headline": journal.headline(journal._row(r))} for r in rows[:URGENT_ITEMS]]
    return {"count": len(rows), "items": items}


def ack(conn, name, principal, seq):
    """Mark the feed read up to `seq`. Refused beyond what was shown; an older ack is a no-op."""
    c = _own(conn, name, principal)
    seq = int(seq)
    if seq > c["shown_through"]:
        raise Refused(f"cannot ack {seq}: the feed was shown only through {c['shown_through']}; "
                      "read it with events_since first")
    if seq > c["acked_seq"]:
        conn.execute("UPDATE consumers SET acked_seq=? WHERE name=?", (seq, name))
    return max(seq, c["acked_seq"])


def show(conn, seq):
    """Peek at one event in full. Moves no bookmark."""
    ev = journal.get(conn, int(seq))
    if ev is None:
        raise FeedError(f"no event {seq} (it may have been pruned)")
    ev["headline"] = journal.headline(ev)
    return ev
