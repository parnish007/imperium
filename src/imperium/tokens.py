"""Bearer tokens (256-bit, stored hashed) and where clients find them (DESIGN §11.2, P2-14)."""
import hashlib
import os
import re
import secrets

from . import fsutil, journal

_SESSION = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class NoCredential(RuntimeError):
    pass


def _digest(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue(conn, principal):
    raw = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO tokens(hash, principal, created) VALUES(?,?,?)",
                 (_digest(raw), principal, journal.now()))
    return raw


def verify(conn, raw):
    """The principal a token belongs to, or None if unknown or revoked."""
    if not raw:
        return None
    r = conn.execute("SELECT principal FROM tokens WHERE hash=? AND revoked IS NULL", (_digest(raw),)).fetchone()
    return r[0] if r else None


def revoke_principal(conn, principal):
    cur = conn.execute("UPDATE tokens SET revoked=? WHERE principal=? AND revoked IS NULL",
                       (journal.now(), principal))
    return cur.rowcount


def revoke_except(conn, principal, keep_raw):
    """Revoke every valid token of `principal` except the one given (finishing a rotation)."""
    cur = conn.execute("UPDATE tokens SET revoked=? WHERE principal=? AND revoked IS NULL AND hash != ?",
                       (journal.now(), principal, _digest(keep_raw)))
    return cur.rowcount


def rotate_owner(store, home):
    """Rotate the owner token without a window where no valid token is on disk [C4].

    1. issue the new token (the old one stays valid) and commit;
    2. write the locator atomically; if that fails, revoke the new token and stop;
    3. revoke every other owner token and commit.
    A crash after step 2 leaves two valid tokens; `finish_rotation` (run at start-up) completes step 3.
    """
    with store.tx() as conn:
        raw = issue(conn, "owner")
    try:
        write_locator(home, "owner", raw)
    except BaseException:
        with store.tx() as conn:
            conn.execute("UPDATE tokens SET revoked=? WHERE hash=?", (journal.now(), _digest(raw)))
        raise
    with store.tx() as conn:
        revoke_except(conn, "owner", raw)
        journal.append(conn, "TOKEN_ROTATED", "NOTICE", caller="owner", data={"principal": "owner"})


def finish_rotation(conn, home):
    """If the owner locator holds a valid token, revoke any other owner token left by an interrupted rotation."""
    raw = read_locator(home, "owner")
    if raw and verify(conn, raw) == "owner":
        n = revoke_except(conn, "owner", raw)
        if n:
            journal.append(conn, "TOKEN_ROTATION_FINISHED", "NOTICE", data={"revoked": n})
        return n
    return 0


def check_principal(principal):
    _locator_name(principal)


def _locator_name(principal):
    if principal == "owner":
        return "owner"
    if principal.startswith("director:"):
        sid = principal.split(":", 1)[1]
        if not _SESSION.match(sid):
            raise ValueError("a Claude Code session id may contain only letters, digits, '-' and '_'")
        return "director-" + sid
    raise ValueError(f"no locator for principal {principal!r}")


def write_locator(home, principal, raw):
    d = os.path.join(home, "tokens")
    os.makedirs(d, exist_ok=True)
    return fsutil.atomic_write(os.path.join(d, _locator_name(principal)), raw, mode=0o600)


def read_locator(home, principal):
    path = os.path.join(home, "tokens", _locator_name(principal))
    try:
        with open(path, encoding="ascii") as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def remove_locator(home, principal):
    try:
        os.remove(os.path.join(home, "tokens", _locator_name(principal)))
    except FileNotFoundError:
        pass


def select_token(home, env=None, as_owner=False):
    """Pick (principal, token) for a client.

    Inside a Claude Code session (CLAUDE_CODE_SESSION_ID set) the director token of that session is used;
    the owner token only with an explicit `--as owner`. There is never a fallback from director to owner.
    """
    env = os.environ if env is None else env
    session = env.get("CLAUDE_CODE_SESSION_ID")
    if as_owner or not session:
        raw = read_locator(home, "owner")
        if not raw:
            raise NoCredential("no owner token found; run `imperium init`")
        return "owner", raw
    principal = "director:" + session
    raw = read_locator(home, principal)
    if not raw:
        raise NoCredential("this Claude Code session has no director token; run `imperium director claim` "
                           "(or pass --as owner if you are the owner)")
    return principal, raw
