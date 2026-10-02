"""Imperium runs git inside a builder's repository, whose `.git/config` and `.gitattributes` the builder controls.
No git command Imperium runs may execute anything the builder configured: clean and smudge filters, external diff
drivers, text conversion, hooks, fsmonitor, or a signing program. Under isolation mode that would be code
execution as the owner."""
import os
import subprocess
import sys
import tempfile
import unittest

from imperium import snapshot

PY = sys.executable.replace("\\", "/")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class TestHostileRepository(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ws = os.path.join(self.tmp, "ws")
        os.makedirs(self.ws)
        self.marker = os.path.join(self.tmp, "PWNED").replace("\\", "/")
        with open(os.path.join(self.ws, "a.txt"), "w", encoding="utf-8") as f:
            f.write("hello\n")
        git(self.ws, "init", "-q")
        git(self.ws, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
        git(self.ws, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        # the builder's hostile configuration
        cmd = f'"{PY}" -c "open(\'{self.marker}\', \'a\').write(\'x\')"'
        for key, value in (("filter.evil.clean", cmd), ("filter.evil.smudge", cmd), ("diff.external", cmd),
                           ("diff.evil.textconv", cmd), ("commit.gpgSign", "true"), ("gpg.program", cmd),
                           ("core.fsmonitor", cmd)):
            git(self.ws, "config", key, value)
        with open(os.path.join(self.ws, ".gitattributes"), "w", encoding="utf-8") as f:
            f.write("* filter=evil diff=evil\n")
        with open(os.path.join(self.ws, "a.txt"), "w", encoding="utf-8") as f:
            f.write("changed\n")

    def assertNotRun(self):
        self.assertFalse(os.path.exists(self.marker), "a builder-configured command ran")

    def test_snapshot_runs_nothing_and_records_the_raw_content(self):
        snap = snapshot.take(self.ws, "refs/imperium/t/base")
        self.assertNotRun()
        self.assertEqual(git(self.ws, "cat-file", "-p", f"{snap['commit']}:a.txt"), "changed")

    def test_check_copy_runs_nothing(self):
        snap = snapshot.take(self.ws, "refs/imperium/t/c")
        with snapshot.Worktree(snap["top"], snap["commit"]) as wt:
            with open(os.path.join(wt, "a.txt"), encoding="utf-8") as f:
                self.assertEqual(f.read(), "changed\n")
        self.assertNotRun()

    def test_diff_runs_nothing(self):
        base = snapshot.take(self.ws, "refs/imperium/t/b")
        with open(os.path.join(self.ws, "a.txt"), "w", encoding="utf-8") as f:
            f.write("again\n")
        cand = snapshot.take(self.ws, "refs/imperium/t/k")
        self.assertIn("again", snapshot.diff(base["top"], base["commit"], cand["commit"]))
        snapshot.changed_files(base["top"], base["commit"], cand["commit"])
        snapshot.diff_stat(base["top"], base["commit"], cand["commit"])
        self.assertNotRun()

    def test_deleted_and_untracked_files(self):
        os.remove(os.path.join(self.ws, "a.txt"))
        with open(os.path.join(self.ws, "new.txt"), "w", encoding="utf-8") as f:
            f.write("n\n")
        snap = snapshot.take(self.ws, "refs/imperium/t/d")
        files = git(self.ws, "ls-tree", "-r", "--name-only", snap["commit"]).splitlines()
        self.assertNotIn("a.txt", files)
        self.assertIn("new.txt", files)
        self.assertNotRun()


if __name__ == "__main__":
    unittest.main()
