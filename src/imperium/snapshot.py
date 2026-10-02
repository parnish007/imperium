"""Code snapshots for rounds (DESIGN §7.4; S4 review D4).

A snapshot is the whole working tree as it is now, uncommitted and unstaged changes included, built in a
temporary index so the builder's own index and branch are never touched. It is wrapped in a commit and kept
under `refs/imperium/<builder>/<round>/...` so `git gc` cannot prune it. Ordinary `git push` does not send those
refs; `git push --mirror` would.

The repository belongs to the builder, and so do its `.git/config` and `.gitattributes`. No git command here may
run anything the builder configured (under isolation mode that would be code execution as the owner):
- files are hashed with `hash-object --no-filters`, never `git add` (clean filters);
- a check's copy is written by Python from raw object contents (`cat-file --batch`), never checked out or
  archived (both apply smudge filters; checkout also runs hooks);
- diffs use `--no-ext-diff --no-textconv`; commits are never signed (`gpg.program`); hooks and fsmonitor are off.
`.imperium/` (round briefs and claim files) is excluded from every snapshot.
"""
import os
import shutil
import stat
import subprocess
import tempfile

EXCLUDE = ".imperium"
IDENT = {"GIT_AUTHOR_NAME": "imperium", "GIT_AUTHOR_EMAIL": "imperium@localhost",
         "GIT_COMMITTER_NAME": "imperium", "GIT_COMMITTER_EMAIL": "imperium@localhost"}
SAFE = ["-c", "core.fsmonitor=false", "-c", "core.autocrlf=false", "-c", "gc.auto=0", "-c", "commit.gpgSign=false",
        "-c", "tag.gpgSign=false", "-c", "core.untrackedCache=false", "-c", "safe.directory=*",
        "-c", "core.splitIndex=false"]


class SnapshotError(RuntimeError):
    pass


def _git(args, cwd, env=None, check=True, timeout=300, data=None, raw=False):
    hooks = os.path.join(tempfile.gettempdir(), "imperium-no-hooks")
    os.makedirs(hooks, exist_ok=True)
    full = ["git", "-c", f"core.hooksPath={hooks}"] + SAFE + args
    e = dict(os.environ)
    for k in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE", "GIT_EXTERNAL_DIFF", "GIT_CONFIG_PARAMETERS"):
        e.pop(k, None)
    e["GIT_CONFIG_NOSYSTEM"] = "1"
    e["GIT_TERMINAL_PROMPT"] = "0"
    e.update(env or {})
    try:
        r = subprocess.run(full, cwd=cwd, env=e, capture_output=True, timeout=timeout, input=data)
    except FileNotFoundError:
        raise SnapshotError("git is not installed") from None
    except subprocess.TimeoutExpired:
        raise SnapshotError(f"git {args[0]} timed out") from None
    if check and r.returncode != 0:
        raise SnapshotError(f"git {args[0]} failed: {r.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return r.stdout if raw else r.stdout.decode("utf-8", "replace").strip()


def repo_root(directory):
    """(toplevel, prefix of `directory` inside it with '/' separators), or raise SnapshotError."""
    if not os.path.isdir(directory):
        raise SnapshotError(f"workspace {directory} does not exist")
    top = _git(["rev-parse", "--show-toplevel"], directory)
    prefix = _git(["rev-parse", "--show-prefix"], directory).rstrip("/")
    return os.path.normpath(top), prefix


def ensure_excluded(top):
    """Keep `.imperium/` out of the builder's own `git status` (one line in .git/info/exclude)."""
    git_dir = _git(["rev-parse", "--git-common-dir"], top)
    if not os.path.isabs(git_dir):
        git_dir = os.path.join(top, git_dir)
    path = os.path.join(git_dir, "info", "exclude")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    if "/" + EXCLUDE + "/" not in lines:
        with open(path, "a", encoding="utf-8") as f:
            f.write(("\n" if lines and lines[-1] else "") + "/" + EXCLUDE + "/\n")


def _excluded(path):
    return path == EXCLUDE or path.startswith(EXCLUDE + "/")


def tree_of_worktree(top):
    """The tree id of the working tree now, built in a temporary index with raw file contents (no filters).
    Returns (tree, HEAD commit or None, number of files)."""
    fd, index = tempfile.mkstemp(prefix="imperium-index-")
    os.close(fd)
    os.remove(index)  # git creates it; an empty file is not a valid index
    env = {"GIT_INDEX_FILE": index}
    try:
        head = _git(["rev-parse", "--verify", "-q", "HEAD"], top, check=False) or None
        if head:
            _git(["read-tree", "HEAD"], top, env)
        modes = {}
        for line in _git(["ls-files", "-s", "-z"], top, env, raw=True).split(b"\0"):
            if line:
                meta, path = line.split(b"\t", 1)
                modes[path.decode("utf-8", "surrogateescape")] = meta.split()[0].decode()
        listed = _git(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], top, env, raw=True)
        paths = sorted({p.decode("utf-8", "surrogateescape") for p in listed.split(b"\0") if p})
        remove, files, links = [], [], []
        for p in paths:
            if _excluded(p):
                if p in modes:
                    remove.append(p)
                continue
            full = os.path.join(top, *p.split("/"))
            if os.path.islink(full):
                links.append(p)
            elif os.path.isfile(full):
                files.append(p)
            elif os.path.isdir(full):
                continue  # a submodule (gitlink): its index entry is kept as it is
            elif p in modes:
                remove.append(p)
        lines = []
        if files:
            ids = _git(["hash-object", "-w", "--no-filters", "--stdin-paths"], top, env,
                       data="\n".join(files).encode("utf-8", "surrogateescape")).split()
            if len(ids) != len(files):
                raise SnapshotError("hash-object returned the wrong number of ids")
            for p, oid in zip(files, ids):
                mode = modes.get(p, "100644")
                if mode not in ("100644", "100755"):
                    mode = "100644"
                if os.name != "nt" and os.stat(os.path.join(top, *p.split("/"))).st_mode & stat.S_IXUSR:
                    mode = "100755"
                lines.append(f"{mode} {oid}\t{p}")
        for p in links:
            target = os.readlink(os.path.join(top, *p.split("/")))
            oid = _git(["hash-object", "-w", "--no-filters", "--stdin"], top, env,
                       data=target.encode("utf-8", "surrogateescape"))
            lines.append(f"120000 {oid}\t{p}")
        if remove:
            _git(["update-index", "--force-remove", "-z", "--stdin"], top, env,
                 data=b"\0".join(p.encode("utf-8", "surrogateescape") for p in remove) + b"\0")
        if lines:
            _git(["update-index", "--index-info"], top, env,
                 data=("\n".join(lines) + "\n").encode("utf-8", "surrogateescape"))
        tree = _git(["write-tree"], top, env)
        return tree, head, len(files) + len(links)
    finally:
        for p in (index, index + ".lock"):
            try:
                os.remove(p)
            except OSError:
                pass


def take(directory, ref):
    """Snapshot the repository holding `directory`; returns {tree, commit, ref, prefix, files, top}."""
    top, prefix = repo_root(directory)
    tree, head, files = tree_of_worktree(top)
    args = ["commit-tree", "--no-gpg-sign", tree, "-m", f"imperium snapshot {ref}"]
    if head:
        args[2:2] = ["-p", head]
    commit = _git(args, top, IDENT)
    _git(["update-ref", ref, commit], top)
    return {"tree": tree, "commit": commit, "ref": ref, "prefix": prefix, "files": files, "top": top}


def blob(top, commit, path):
    """The blob id of `path` (repo-relative, '/' separators) in `commit`, or None if absent."""
    out = _git(["rev-parse", "-q", "--verify", f"{commit}:{path}"], top, check=False)
    return out or None


def hash_file(top, path):
    """The blob id the file at `path` (repo-relative) has in the working tree now (raw content), or None."""
    full = os.path.join(top, *path.split("/"))
    if not os.path.isfile(full):
        return None
    return _git(["hash-object", "--no-filters", "--", full], top)


DIFF = ["--no-ext-diff", "--no-textconv", "--no-color"]


def changed_files(top, base_commit, cand):
    out = _git(["diff", *DIFF, "--name-status", "--no-renames", base_commit, cand], top)
    return [tuple(line.split("\t", 1)) for line in out.splitlines() if "\t" in line]


def diff_stat(top, base_commit, cand):
    return _git(["diff", *DIFF, "--stat", base_commit, cand], top)


def diff(top, base_commit, cand):
    return _git(["diff", *DIFF, base_commit, cand], top)


class Worktree:
    """A temporary copy of a commit's files, written by Python from raw object contents (nothing in the
    repository's configuration runs), removed on exit."""

    def __init__(self, top, commit):
        self.top, self.commit = top, commit
        self.path = None

    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="imperium-check-")
        root = os.path.realpath(self.path)
        listing = _git(["ls-tree", "-r", "-z", "--full-tree", self.commit], self.top, raw=True)
        entries = []
        for item in listing.split(b"\0"):
            if not item:
                continue
            meta, path = item.split(b"\t", 1)
            mode, kind, oid = meta.decode().split()
            if kind != "blob":
                continue  # submodules are not part of a check's copy
            entries.append((mode, oid, path.decode("utf-8", "surrogateescape")))
        # raw object contents: `cat-file --batch` never applies filters (archive and checkout both would)
        out = _git(["cat-file", "--batch"], self.top, raw=True, timeout=600,
                   data=("\n".join(oid for _, oid, _ in entries) + "\n").encode())
        pos = 0
        for mode, oid, path in entries:
            nl = out.index(b"\n", pos)
            size = int(out[pos:nl].split()[2])
            content = out[nl + 1:nl + 1 + size]
            pos = nl + 1 + size + 1
            dest = os.path.realpath(os.path.join(self.path, *path.split("/")))
            if not dest.startswith(root + os.sep):
                raise SnapshotError(f"unsafe path in the snapshot: {path}")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            # a symlink (mode 120000) is written as a plain file holding its target: never followed
            with open(dest, "wb") as f:
                f.write(content)
            if mode == "100755" and os.name != "nt":
                os.chmod(dest, 0o755)
        return self.path

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)
        return False
