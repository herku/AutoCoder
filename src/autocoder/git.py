from __future__ import annotations

import atexit
import os
import re
import subprocess
import threading
from pathlib import Path

from autocoder.types import LockError

# `git worktree add/remove/prune` all mutate the shared .git worktree
# registry; concurrent workers under --parallel must serialize those calls.
_WORKTREE_REGISTRY_LOCK = threading.Lock()

# Branches AutoCoder itself creates: feat/<issue-num>[-slug], the legacy ai/
# prefix, and per-run worktree branches. Cleanup must never sweep broader
# than this — plain feat/* would destroy human branches on a very common
# naming convention.
_AUTOCODER_BRANCH_RE = re.compile(r"^(feat/\d+(-|$)|ai/|autocoder-wt-)")

_PLAN_ONLY_PATTERN = re.compile(
    r"^(PLAN[-_].*\.md|plan[-_].*\.md|TODO[-_].*\.md|IMPLEMENTATION[-_].*\.md)$",
    re.IGNORECASE,
)


def _is_plan_only_file(filepath: str) -> bool:
    """Check if a file is a plan/documentation artifact, not source code."""
    basename = os.path.basename(filepath)
    return bool(_PLAN_ONLY_PATTERN.match(basename))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # e.g. PermissionError — exists but owned by another user
        return True
    return True


def ensure_autocoder_ignored(repo_path: str) -> None:
    """Make {repo}/.autocoder self-ignoring so its contents (lockfile, caches,
    plan files, downloaded issue images) can never be swept into a commit by
    `git add -A` — the target repo's own .gitignore can't be relied on."""
    gitignore = Path(repo_path) / ".autocoder" / ".gitignore"
    try:
        if not gitignore.exists():
            gitignore.parent.mkdir(parents=True, exist_ok=True)
            gitignore.write_text("*\n")
    except OSError:
        pass


def _slugify(title: str, max_len: int = 40) -> str:
    """Convert issue title to a branch-name-safe slug."""
    # Remove emoji and special chars, lowercase
    slug = re.sub(r"[^\w\s-]", "", title).strip().lower()
    # Replace whitespace/underscores with hyphens
    slug = re.sub(r"[\s_]+", "-", slug)
    # Remove leading/trailing hyphens
    slug = slug.strip("-")
    # Truncate at word boundary
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit("-", 1)[0]
    return slug or "fix"


class GitOps:
    def __init__(self, repo_path: str):
        self.repo_path = repo_path
        self._lockfile = Path(repo_path) / ".autocoder" / ".autocoder.lock"
        self._lock_held = False
        self._atexit_registered = False

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git"] + list(args),
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            check=check,
        )

    def _read_lock_pid(self) -> int | None:
        try:
            return int(self._lockfile.read_text().strip())
        except (OSError, ValueError):
            return None

    def acquire_lock(self) -> None:
        if self._lock_held:
            return
        self._lockfile.parent.mkdir(parents=True, exist_ok=True)
        ensure_autocoder_ignored(self.repo_path)
        for attempt in (1, 2):
            try:
                # O_EXCL makes creation atomic — two racing processes can't
                # both pass an exists() check and clobber each other's lock.
                fd = os.open(self._lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                pid = self._read_lock_pid()
                # Only a parsed PID confirmed dead is stale. Unreadable/empty
                # content may be a live lock caught between another process's
                # O_EXCL create and its PID write — never auto-remove those.
                if attempt == 1 and pid is not None and not _pid_alive(pid):
                    print(f"  Removing stale AutoCoder lock from dead PID {pid}.")
                    try:
                        self._lockfile.unlink()
                    except FileNotFoundError:
                        pass  # another process cleaned it first; O_EXCL decides
                    continue
                raise LockError(
                    f"Another AutoCoder instance is running "
                    f"(PID {pid if pid is not None else 'unknown'}). "
                    f"Remove {self._lockfile} if this is stale."
                )
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            self._lock_held = True
            if not self._atexit_registered:
                atexit.register(self.release_lock)
                self._atexit_registered = True
            return

    def release_lock(self) -> None:
        if not self._lock_held:
            return
        self._lock_held = False
        try:
            # Ownership check: never delete a lock another process re-acquired
            # after ours was manually removed.
            if self._lockfile.read_text().strip() == str(os.getpid()):
                self._lockfile.unlink()
        except OSError:
            pass

    def assert_clean(self) -> None:
        result = self._run("status", "--porcelain")
        # Filter out .autocoder/ directory (lock file, cache) from dirty check
        dirty = [
            line for line in result.stdout.strip().split("\n")
            if line.strip() and ".autocoder/" not in line
        ]
        if dirty:
            raise SystemExit(
                f"Error: Working directory is not clean:\n" + "\n".join(dirty) + "\n"
                "Commit or stash changes before running AutoCoder."
            )

    def has_commits(self) -> bool:
        result = self._run("rev-parse", "HEAD", check=False)
        return result.returncode == 0

    def get_main_branch(self) -> str:
        for name in ("main", "master"):
            result = self._run("rev-parse", "--verify", name, check=False)
            if result.returncode == 0:
                return name
        # No main/master yet — check current branch name
        result = self._run("branch", "--show-current", check=False)
        current = result.stdout.strip()
        if current:
            return current
        raise SystemExit("Error: Could not find main or master branch")

    def get_head_sha(self) -> str:
        result = self._run("rev-parse", "HEAD")
        return result.stdout.strip()

    def save_checkpoint(self) -> str:
        if not self.has_commits():
            return ""  # Empty repo, no checkpoint possible
        return self.get_head_sha()

    def create_branch(self, issue_num: int, title: str = "") -> str:
        slug = _slugify(title) if title else str(issue_num)
        branch = f"feat/{issue_num}-{slug}" if slug != str(issue_num) else f"feat/{issue_num}"
        # Must leave the branch before we can delete it
        current = self._run("branch", "--show-current", check=False).stdout.strip()
        if current == branch:
            # On the target branch — need to switch away first
            main = self.get_main_branch()
            if main == branch:
                # Only branch is the target — create main, switch to it
                self._run("checkout", "-b", "main", check=False)
            else:
                self._run("checkout", main, check=False)
        elif self.has_commits():
            main = self.get_main_branch()
            self._run("checkout", main, check=False)
        self._run("branch", "-D", branch, check=False)
        self._run("checkout", "-b", branch)
        return branch

    def rollback(self, sha: str) -> None:
        if not sha:
            # Empty repo checkpoint — remove all tracked files
            self._run("rm", "-rf", ".", check=False)
            return
        self._run("reset", "--hard", sha)

    def checkout_main(self) -> None:
        main = self.get_main_branch()
        self._run("checkout", main, check=False)

    def delete_branch(self, branch: str) -> None:
        self._run("branch", "-D", branch, check=False)

    def commit_all(self, message: str) -> None:
        # .autocoder/ holds run-state (lockfile, caches, plan files, downloaded
        # issue images) that must never ride along into a commit, even when the
        # target repo's .gitignore doesn't cover it.
        self._run("add", "-A", "--", ".", ":(exclude).autocoder")
        # Check if there are staged changes to commit
        status = self._run("diff", "--cached", "--quiet", check=False)
        if status.returncode == 0:
            raise RuntimeError("Agent produced no code changes to commit")

        # Guard against plan-only commits (no actual source changes)
        staged = self._run("diff", "--cached", "--name-only")
        files = [f for f in staged.stdout.strip().split("\n") if f]
        source_files = [f for f in files if not _is_plan_only_file(f)]
        if not source_files:
            raise RuntimeError(
                "Agent produced only plan/documentation files, no source code changes"
            )

        self._run("commit", "-m", message)

    def push_branch(self, branch: str) -> None:
        self._run("push", "-u", "origin", branch, "--force-with-lease")

    def diff_full(self, base: str | None = None) -> str:
        target = base or self.get_main_branch()
        result = self._run("diff", target, check=False)
        return result.stdout

    def diff_stats(self) -> str:
        main = self.get_main_branch()
        result = self._run("diff", "--stat", main, check=False)
        return result.stdout.strip()

    def diff_files(self) -> list[str]:
        main = self.get_main_branch()
        result = self._run("diff", "--name-only", main, check=False)
        return [f for f in result.stdout.strip().split("\n") if f]

    def diff_last_commit_stats(self) -> str:
        """Stat summary of the most recent commit's changes."""
        result = self._run("diff", "HEAD~1", "--stat", check=False)
        return result.stdout.strip()

    def cleanup_orphan_branches(self) -> None:
        """Delete leftover branches from crashed AutoCoder runs.

        Only branches matching AutoCoder's own naming shapes are removed
        (see _AUTOCODER_BRANCH_RE) — never a blanket feat/* sweep.
        """
        result = self._run("branch", "--list", "--format=%(refname:short)")
        for branch in (b.strip() for b in result.stdout.splitlines()):
            if branch and _AUTOCODER_BRANCH_RE.match(branch):
                self._run("branch", "-D", branch, check=False)

    def create_worktree(self, path: str, branch: str, base: str | None = None) -> None:
        """Create a fresh worktree at `path`, on a new branch off `base` (default: main).

        Caller is responsible for `remove_worktree` on completion.
        """
        target = base or self.get_main_branch()
        with _WORKTREE_REGISTRY_LOCK:
            # Force: drop any pre-existing branch with the same name
            self._run("branch", "-D", branch, check=False)
            self._run("worktree", "add", "-b", branch, path, target)

    def remove_worktree(self, path: str) -> None:
        """Force-remove a worktree at `path`. Safe to call multiple times."""
        with _WORKTREE_REGISTRY_LOCK:
            self._run("worktree", "remove", "--force", path, check=False)

    def prune_worktrees(self) -> None:
        """Clean up worktree administrative records for removed directories."""
        with _WORKTREE_REGISTRY_LOCK:
            self._run("worktree", "prune", check=False)
