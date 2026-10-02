"""The runtime directory (`~/.imperium` or `IMPERIUM_HOME`) and its protection (DESIGN §11.2)."""
import os
import subprocess

SYNCED_MARKERS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud")


def home(override=None, env=None):
    env = os.environ if env is None else env
    return os.path.abspath(override or env.get("IMPERIUM_HOME") or os.path.join(os.path.expanduser("~"), ".imperium"))


def db(h):
    return os.path.join(h, "imperium.db")


def archive(h):
    return os.path.join(h, "archive")


def backups(h):
    return os.path.join(h, "backups")


def logs(h):
    return os.path.join(h, "logs")


def daemon_json(h):
    return os.path.join(h, "daemon.json")


def lockfile(h):
    return os.path.join(h, "imperium.lock")


def config(h):
    return os.path.join(h, "imperium.toml")


def ensure_home(h):
    os.makedirs(h, exist_ok=True)
    secure_dir(h)


def _windows_user():
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    return f"{domain}\\{user}" if domain else user


def secure_dir(path):
    """Owner-only: mode 0700 on POSIX; on Windows, an ACL granting only the current user."""
    if os.name == "posix":
        os.chmod(path, 0o700)
        return True
    user = _windows_user()
    if not user:
        return False
    r = subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
                       capture_output=True, text=True)
    return r.returncode == 0


def check_private(path):
    """(status, detail) for the doctor."""
    if os.name == "posix":
        mode = os.stat(path).st_mode & 0o777
        if mode & 0o077:
            return "WARN", f"mode {oct(mode)}: others can read it; run `chmod 700 {path}`"
        return "OK", f"mode {oct(mode)}"
    r = subprocess.run(["icacls", path], capture_output=True, text=True)
    if r.returncode != 0:
        return "WARN", "could not read the folder's permissions (icacls failed)"
    user = _windows_user().lower()
    others = []
    for line in r.stdout.splitlines()[:-1]:
        line = line.replace(path, "", 1).strip()
        if ":" not in line:
            continue
        who = line.split(":", 1)[0].strip().lower()
        if who and who != user:
            others.append(who)
    if others:
        return "WARN", "others have access: " + ", ".join(sorted(set(others))) + "; run `imperium init` to fix"
    return "OK", "only the current user has access"


def check_local(path):
    low = path.lower()
    for m in SYNCED_MARKERS:
        if m in low:
            return "WARN", f"the folder looks synced ({m}); sync tools can corrupt a live database"
    if os.name == "nt":
        import ctypes
        root = os.path.splitdrive(path)[0] + "\\"
        kind = ctypes.windll.kernel32.GetDriveTypeW(root)
        if kind != 3:  # DRIVE_FIXED
            return "WARN", f"drive {root} is not a fixed local disk (type {kind})"
        return "OK", f"fixed local disk {root}"
    return "UNVERIFIED", "local-disk check not implemented on this platform"
