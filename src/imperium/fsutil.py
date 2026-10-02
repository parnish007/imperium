"""Crash-safe file writes. Every file whose content matters (credentials, daemon info, archives, backups)
is written to a temporary file in the same directory, flushed and fsynced, renamed over the target, and
the directory is fsynced, so a crash leaves either the old content or the new, never a mixture."""
import os
import tempfile


def fsync_dir(path):
    if os.name != "posix":  # Windows cannot open a directory for fsync; NTFS journals the rename itself
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_file(path):
    with open(path, "rb+") as f:
        os.fsync(f.fileno())


def atomic_write(path, data, mode=0o600):
    """Replace `path` with `data` (str or bytes) atomically. Creates the file with `mode` on POSIX."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        if os.name == "posix":
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    fsync_dir(directory)
    return path
