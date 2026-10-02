"""Builder registry (DESIGN §4 Builder, §10.3). Credentials are stored as a variable name or file path, never a value."""
import os
import re

from . import journal, opencode

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
FIELDS = ("name", "adapter", "endpoint", "session_id", "directory", "password_env", "password_file",
          "opencode_version", "allowed_version", "paused", "created")


class BuilderError(ValueError):
    pass


class Conflict(BuilderError):
    pass


def norm_dir(d):
    return os.path.normcase(os.path.normpath(d))


def overlaps(a, b):
    a, b = norm_dir(a), norm_dir(b)
    sep = os.sep
    return a == b or a.startswith(b.rstrip(sep) + sep) or b.startswith(a.rstrip(sep) + sep)


def _row(r):
    return {k: r[k] for k in FIELDS}


def list_(conn):
    return [_row(r) for r in conn.execute("SELECT * FROM builders ORDER BY name")]


def get(conn, name):
    r = conn.execute("SELECT * FROM builders WHERE name=?", (name,)).fetchone()
    if not r:
        raise BuilderError(f"no builder named {name!r}")
    return _row(r)


def validate(name, endpoint):
    if not NAME.match(name or ""):
        raise BuilderError("a builder name is 1-32 characters: lowercase letters, digits, '-' and '_', "
                           "starting with a letter or digit")
    return opencode.canonical_endpoint(endpoint)


def add(conn, *, name, endpoint, session_id, directory, password_env=None, password_file=None, version=None,
        caller="owner"):
    endpoint = validate(name, endpoint)
    if conn.execute("SELECT 1 FROM builders WHERE name=?", (name,)).fetchone():
        raise Conflict(f"a builder named {name!r} already exists")
    dup = conn.execute("SELECT name FROM builders WHERE adapter='opencode-http' AND endpoint=? AND session_id=?",
                       (endpoint, session_id)).fetchone()
    if dup:
        raise Conflict(f"session {session_id} on {endpoint} is already registered as {dup[0]!r}")
    for b in list_(conn):
        if overlaps(b["directory"], directory):
            raise Conflict(f"workspace {directory} overlaps builder {b['name']!r} ({b['directory']}); "
                           "two builders in one workspace are not supported in v1")
    conn.execute("INSERT INTO builders(name, adapter, endpoint, session_id, directory, password_env, password_file, "
                 "opencode_version, allowed_version, paused, created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (name, "opencode-http", endpoint, session_id, directory, password_env, password_file, version,
                  None, 0, journal.now()))
    journal.append(conn, "BUILDER_ADDED", "NOTICE", builder=name, caller=caller,
                   data={"endpoint": endpoint, "session_id": session_id, "directory": directory,
                         "opencode_version": version,
                         "credential": "env:" + password_env if password_env else
                         ("file" if password_file else "none")})
    return get(conn, name)


def remove(conn, name, caller="owner"):
    get(conn, name)
    conn.execute("DELETE FROM builders WHERE name=?", (name,))
    conn.execute("DELETE FROM checkpoints WHERE builder=?", (name,))
    journal.append(conn, "BUILDER_REMOVED", "NOTICE", builder=name, caller=caller)


def password(b, env=None):
    """The builder's server password, read at use time from its variable or file; None when not configured."""
    env = os.environ if env is None else env
    if b.get("password_env"):
        return env.get(b["password_env"])
    if b.get("password_file"):
        with open(b["password_file"], encoding="utf-8") as f:
            return f.read().strip()
    return None
