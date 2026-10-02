"""Backup with SQLite's online backup API, and restore into observe-only mode (DESIGN §11.3, P2-12).

A file copy of a WAL database can miss committed data, so backups never copy files. A restored
database has forgotten everything after the backup, including client keys, so the daemon starts
observe-only after a restore until the owner confirms.
"""
import os
import pathlib
import sqlite3

from . import journal
from .store import Store, meta_get, meta_set


class RestoreError(RuntimeError):
    pass


def _fsync(path):
    with open(path, "rb+") as f:
        os.fsync(f.fileno())


def backup(store, dest):
    """Write a consistent copy of the live database to `dest` (atomic replace)."""
    tmp = str(dest) + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    with store.read() as conn:
        dst = sqlite3.connect(tmp)
        try:
            conn.backup(dst)
        finally:
            dst.close()
    _fsync(tmp)
    os.replace(tmp, dest)
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
            return conn
        except BaseException:
            conn.close()
            raise
    except sqlite3.DatabaseError as e:
        raise RestoreError(f"{src} is not a readable Imperium database: {e}") from e


def restore(src, db_path):
    """Replace the database at `db_path` with the backup `src`. The daemon must not be running.

    The current database is first saved beside it as `<db>.pre-restore-<n>` (with the backup API).
    """
    src_conn = _validate(src)
    try:
        if os.path.exists(db_path):
            n = 1
            while os.path.exists(f"{db_path}.pre-restore-{n}"):
                n += 1
            old = Store(db_path)
            try:
                backup(old, f"{db_path}.pre-restore-{n}")
            finally:
                old.close()
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(db_path + suffix):
                    os.remove(db_path + suffix)
        dst = sqlite3.connect(db_path)
        try:
            src_conn.backup(dst)
        finally:
            dst.close()
    finally:
        src_conn.close()
    store = Store(db_path)
    try:
        with store.tx() as conn:
            head_seq, _ = journal.head(conn)
            journal.append(conn, "RESTORED", "ACTION", caller="owner",
                           data={"backup": os.path.basename(str(src)), "backup_head_seq": head_seq,
                                 "dispatch": "observe-only until the owner confirms"})
            meta_set(conn, "observe_only", "restored")
    finally:
        store.close()


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
