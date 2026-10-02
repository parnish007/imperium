"""Retention: prune only a contiguous oldest prefix, through the archive protocol (DESIGN §5, P2-21).

Protocol: (1) write `<name>.tmp`, flush, fsync; (2) re-read it and check the row count and the boundary
hash; (3) rename to the final name and fsync the directory; (4) in one transaction append
JOURNAL_PRUNED and delete the rows. `check_archives` runs at start-up and repairs or reports what a
crash between the steps left behind.
"""
import gzip
import hashlib
import json
import os

from . import journal

PROTECTED = ("ACTION", "CRITICAL")


class PruneRefused(ValueError):
    pass


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_dir(path):
    if os.name != "posix":  # Windows cannot open a directory for fsync; NTFS journals the rename
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def limit(conn):
    """The highest seq that may be pruned now, and why it is not higher."""
    head_seq, _ = journal.head(conn)
    lim, why = head_seq, "head"
    consumers = conn.execute("SELECT name, floor, acked_seq FROM consumers").fetchall()
    for c in consumers:
        if c["acked_seq"] < lim:
            lim, why = c["acked_seq"], f"bookmark of consumer {c['name']!r}"
    for sev in PROTECTED:
        r = journal.RANK[sev]
        readers = [c["acked_seq"] for c in consumers if c["floor"] <= r]
        acked = min(readers) if readers else 0
        first = conn.execute("SELECT MIN(seq) FROM events WHERE severity=? AND seq > ?", (r, acked)).fetchone()[0]
        if first is not None and first - 1 < lim:
            lim, why = first - 1, f"unacknowledged {sev} event #{first}"
    return lim, why


def prune(store, archive_dir, through, *, caller="owner"):
    """Prune events with seq <= `through`. Owner-only at the API layer."""
    through = int(through)
    with store.read() as conn:
        lo = journal.boundary(conn)[0]
        lim, why = limit(conn)
        if through > lim:
            raise PruneRefused(f"cannot prune through {through}: limited to {lim} by the {why}")
        if through <= lo:
            raise PruneRefused(f"already pruned through {lo}")
        rows = [journal.raw_row(r) for r in
                conn.execute("SELECT * FROM events WHERE seq > ? AND seq <= ? ORDER BY seq", (lo, through))]
    if not rows or rows[-1]["seq"] != through:
        raise PruneRefused(f"event {through} does not exist")
    boundary_hash = rows[-1]["hash"]
    os.makedirs(archive_dir, exist_ok=True)
    name = f"{rows[0]['seq']:012d}-{through:012d}.jsonl.gz"
    final = os.path.join(archive_dir, name)
    tmp = final + ".tmp"
    with open(tmp, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            for r in rows:
                gz.write((json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        raw.flush()
        os.fsync(raw.fileno())
    with gzip.open(tmp, "rt", encoding="utf-8") as f:
        back = [json.loads(line) for line in f]
    if len(back) != len(rows) or back[-1]["hash"] != boundary_hash:
        os.remove(tmp)
        raise PruneRefused("archive re-read did not match; nothing pruned")
    os.replace(tmp, final)
    _fsync_dir(archive_dir)
    digest = sha256_file(final)
    _commit_prune(store, through, boundary_hash, name, digest, caller)
    return {"boundary_seq": through, "boundary_hash": boundary_hash, "archive": name,
            "archive_sha256": digest, "events": len(rows)}


def _commit_prune(store, through, boundary_hash, name, digest, caller):
    with store.tx() as conn:
        lim, why = limit(conn)  # re-checked inside the transaction
        if through > lim:
            raise PruneRefused(f"cannot prune through {through}: limited to {lim} by the {why}")
        journal.append(conn, "JOURNAL_PRUNED", "NOTICE", caller=caller,
                       data={"boundary_seq": through, "boundary_hash": boundary_hash,
                             "archive": name, "archive_sha256": digest})
        conn.execute("INSERT INTO prune_log(boundary_seq, boundary_hash, archive, archive_sha256, ts) "
                     "VALUES(?,?,?,?,?)", (through, boundary_hash, name, digest, journal.now()))
        conn.execute("DELETE FROM events WHERE seq <= ?", (through,))


def check_archives(store, archive_dir):
    """Start-up check: remove leftovers of an interrupted prune; report missing or altered archives."""
    report = {"removed": [], "missing": [], "mismatched": []}
    with store.read() as conn:
        logged = {r["archive"]: r["archive_sha256"] for r in conn.execute("SELECT archive, archive_sha256 FROM prune_log")}
    present = sorted(os.listdir(archive_dir)) if os.path.isdir(archive_dir) else []
    for n in present:
        if not (n.endswith(".jsonl.gz") or n.endswith(".jsonl.gz.tmp")):
            continue  # never delete a file Imperium did not write
        if n.endswith(".tmp") or n not in logged:
            os.remove(os.path.join(archive_dir, n))
            report["removed"].append(n)
    for n, digest in sorted(logged.items()):
        path = os.path.join(archive_dir, n)
        if not os.path.exists(path):
            report["missing"].append(n)
        elif sha256_file(path) != digest:
            report["mismatched"].append(n)
    if any(report.values()):
        with store.tx() as conn:
            for n in report["removed"]:
                journal.append(conn, "ARCHIVE_ORPHAN_REMOVED", "NOTICE", data={"archive": n})
            for n in report["missing"]:
                journal.append(conn, "ARCHIVE_MISSING", "CRITICAL", data={"archive": n})
            for n in report["mismatched"]:
                journal.append(conn, "ARCHIVE_MISMATCH", "CRITICAL", data={"archive": n})
    return report
