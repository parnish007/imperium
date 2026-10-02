"""SQLite store: one write connection, WAL, synchronous=FULL, fail closed on write errors (DESIGN §3)."""
import contextlib
import sqlite3
import threading

SCHEMA = [
    # version 1
    """
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE events(
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        builder TEXT,
        type TEXT NOT NULL,
        severity INTEGER NOT NULL,
        data TEXT NOT NULL,
        untrusted TEXT,
        source_key TEXT UNIQUE,
        caller TEXT,
        prev_hash TEXT NOT NULL,
        hash TEXT NOT NULL
    );
    CREATE INDEX events_severity ON events(severity, seq);
    CREATE TABLE consumers(
        name TEXT PRIMARY KEY,
        principal TEXT NOT NULL,
        floor INTEGER NOT NULL,
        acked_seq INTEGER NOT NULL,
        shown_through INTEGER NOT NULL,
        created TEXT NOT NULL
    );
    CREATE TABLE prune_log(
        boundary_seq INTEGER PRIMARY KEY,
        boundary_hash TEXT NOT NULL,
        archive TEXT NOT NULL UNIQUE,
        archive_sha256 TEXT NOT NULL,
        ts TEXT NOT NULL
    );
    CREATE TABLE tokens(
        hash TEXT PRIMARY KEY,
        principal TEXT NOT NULL,
        created TEXT NOT NULL,
        revoked TEXT
    );
    CREATE TABLE call_audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        principal TEXT,
        peer_pid INTEGER,
        route TEXT NOT NULL,
        result TEXT NOT NULL
    );
    """,
]


class StoreFailed(RuntimeError):
    """The store refused a write because an earlier write failed; the daemon is failing closed."""


class Store:
    SCHEMA_VERSION = len(SCHEMA)

    def __init__(self, path, factory=None):
        self.path = path
        self.failed = None
        self._lock = threading.RLock()
        kw = {"factory": factory} if factory else {}
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, **kw)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self):
        with self._lock:
            has_meta = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
            current = 0
            if has_meta:
                row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                current = int(row[0]) if row else 0
            if current > self.SCHEMA_VERSION:
                raise RuntimeError(f"database schema {current} is newer than this Imperium ({self.SCHEMA_VERSION})")
            for version in range(current + 1, self.SCHEMA_VERSION + 1):
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in SCHEMA[version - 1].split(";"):
                        if stmt.strip():
                            self.conn.execute(stmt)
                    self.conn.execute(
                        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(version),))
                    self.conn.execute("COMMIT")
                except BaseException:
                    self.conn.execute("ROLLBACK")
                    raise

    @contextlib.contextmanager
    def tx(self):
        """One write transaction. A database error other than a constraint refusal fails the store closed."""
        with self._lock:
            if self.failed:
                raise StoreFailed(self.failed)
            try:
                self.conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as e:
                self.failed = f"journal write failed: {e}"
                raise StoreFailed(self.failed) from e
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except sqlite3.IntegrityError:
                self._rollback()
                raise
            except sqlite3.Error as e:
                self._rollback()
                self.failed = f"journal write failed: {e}"
                raise StoreFailed(self.failed) from e
            except BaseException:
                self._rollback()
                raise

    def _rollback(self):
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    @contextlib.contextmanager
    def read(self):
        with self._lock:
            yield self.conn

    def close(self):
        with self._lock:
            self.conn.close()


def meta_get(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(conn, key, value):
    if value is None:
        conn.execute("DELETE FROM meta WHERE key=?", (key,))
    else:
        conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
