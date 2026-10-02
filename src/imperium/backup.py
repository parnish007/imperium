"""Backup with SQLite's online backup API, and restore into observe-only mode (DESIGN §11.3, P2-12).

A file copy of a WAL database can miss committed data, so backups never copy files. A restored
database has forgotten everything after the backup, including client keys, so the daemon starts
observe-only after a restore until the owner confirms.
"""
import os
import pathlib
import sqlite3

from . import fsutil, journal, paths
from .store import Store, meta_get, meta_set


class RestoreError(RuntimeError):
    pass


def check_destination(home, dest):
    """Refuse destinations that could replace live runtime files [C5]: anything inside the runtime directory
    except its `backups/` folder, and any existing file (backups never overwrite)."""
    real = os.path.realpath(dest)
    home_r = os.path.realpath(home)
    backups_r = os.path.realpath(paths.backups(home))
    inside = lambda p, d: os.path.normcase(p).startswith(os.path.normcase(d) + os.sep)
    if inside(real, home_r) and not inside(real, backups_r):
        raise ValueError(f"backup destination {dest} is inside the runtime directory; use a path outside it "
                         "or leave --to empty for the backups folder")
    if os.path.exists(real):
        raise ValueError(f"{dest} already exists; backups never overwrite a file")
    if not os.path.isdir(os.path.dirname(real)):
        raise ValueError(f"the folder for {dest} does not exist")
    return real


def backup(store, dest):
    """Write a consistent copy of the live database to `dest`, which must not exist yet."""
    tmp = str(dest) + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    with store.read() as conn:
        dst = sqlite3.connect(tmp)
        try:
            conn.backup(dst)
        finally:
            dst.close()
    fsutil.fsync_file(tmp)
    os.replace(tmp, dest)
    fsutil.fsync_dir(os.path.dirname(os.path.abspath(dest)))
    return str(dest)


def _validate(src):
    try:
        conn = sqlite3.connect(pathlib.Path(src).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RestoreError(f"{src} failed SQLite's integrity check")
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if not row:
                raise RestoreError(f"{src} is not an Imperium database")
            if int(row[0]) > Store.SCHEMA_VERSION:
                raise RestoreError(f"{src} has schema {row[0]}, newer than this Imperium ({Store.SCHEMA_VERSION})")
            conn.row_factory = sqlite3.Row
            return conn
        except BaseException:
            conn.close()
            raise
    except sqlite3.DatabaseError as e:
        raise RestoreError(f"{src} is not a readable Imperium database: {e}") from e


def problems(conn):
    """Imperium's own invariants, beyond SQLite's structure [C7]: the journal chain from its boundary, the
    head record, and every feed's bookmarks (acked <= shown <= head)."""
    found = []
    v = journal.verify_chain(conn)
    if not v.ok:
        found.append(f"journal chain broken at event {v.first_bad}: {v.reason}")
    head_seq, _ = journal.head(conn)
    for c in conn.execute("SELECT name, acked_seq, shown_through FROM consumers"):
        if not (0 <= c["acked_seq"] <= c["shown_through"] <= head_seq):
            found.append(f"feed {c['name']!r} has bookmarks outside the journal "
                         f"(acked {c['acked_seq']}, shown {c['shown_through']}, head {head_seq})")
    return found


def restore(src, db_path, force=False):
    """Replace the database at `db_path` with the backup `src`. The daemon must not be running.

    The backup must pass SQLite's integrity check and Imperium's invariants; with `force` (owner) a backup
    that fails them is restored into quarantine instead. Crash-safe order: the backup is copied to a file beside
    the database and marked observe-only (and quarantined if forced) there; the current database is saved as
    `<db>.pre-restore-<n>`; only then is the prepared file renamed over the database in one step. A crash at any
    point leaves either the old database or the fully prepared restored one, never neither.
    """
    src_conn = _validate(src)
    found = []
    try:
        if src_conn.execute("SELECT 1 FROM sqlite_master WHERE name='consumers'").fetchone():
            found = problems(src_conn)
    except sqlite3.DatabaseError as e:
        found = [f"cannot check the journal: {e}"]
    if found and not force:
        src_conn.close()
        raise RestoreError("backup refused: " + "; ".join(found))
    prepared = db_path + ".restore-prepared"
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(prepared + suffix):
            os.remove(prepared + suffix)
    try:
        dst = sqlite3.connect(prepared)
        try:
            src_conn.backup(dst)
        finally:
            dst.close()
    finally:
        src_conn.close()
    store = Store(prepared)
    try:
        with store.tx() as conn:
            head_seq, _ = journal.head(conn)
            journal.append(conn, "RESTORED", "ACTION", caller="owner",
                           data={"backup": os.path.basename(str(src)), "backup_head_seq": head_seq,
                                 "dispatch": "observe-only until the owner confirms", "problems": found})
            meta_set(conn, "observe_only", "restored")
            if found:
                meta_set(conn, "quarantine", "restored a backup that failed Imperium's checks: " + "; ".join(found))
        store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        store.close()
    _settle(prepared)
    fsutil.fsync_file(prepared)
    if os.path.exists(db_path):
        n = 1
        while os.path.exists(f"{db_path}.pre-restore-{n}"):
            n += 1
        old = Store(db_path)
        try:
            backup(old, f"{db_path}.pre-restore-{n}")
            old.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            old.close()
        _settle(db_path)
    os.replace(prepared, db_path)
    fsutil.fsync_dir(os.path.dirname(os.path.abspath(db_path)))


def _settle(path):
    """After a clean close and a TRUNCATE checkpoint the side files hold nothing; remove them so they cannot
    be applied to a different main file."""
    for suffix in ("-wal", "-shm"):
        side = path + suffix
        if os.path.exists(side):
            if suffix == "-wal" and os.path.getsize(side) > 0:
                raise RestoreError(f"{side} still holds data after a checkpoint; is the daemon running?")
            os.remove(side)


def observe_only(store):
    with store.read() as conn:
        return meta_get(conn, "observe_only")


def confirm_restore(store, by):
    with store.tx() as conn:
        if meta_get(conn, "observe_only") is None:
            return False
        meta_set(conn, "observe_only", None)
        journal.append(conn, "RESTORE_CONFIRMED", "NOTICE", caller=by)
        return True
