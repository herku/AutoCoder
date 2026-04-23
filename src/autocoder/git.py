from __future__ import annotations

import atexit
import os
import re
import subprocess
from pathlib import Path

from autocoder.types import LockError

_PLAN_ONLY_PATTERN = re.compile(
    r"^(PLAN[-_].*\.md|plan[-_].*\.md|TODO[-_].*\.md|IMPLEMENTATION[-_].*\.md)$",
    re.IGNORECASE,
)


def _is_plan_only_file(filepath: str) -> bool:
    """Check if a file is a plan/documentation artifact, not source code."""
    basename = os.path.basename(filepath)
    return bool(_PLAN_ONLY_PATTERN.match(basename))


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

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git"] + list(args),
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            check=check,
        )

    def acquire_lock(self) -> None:
        if self._lockfile.exists():
            pid = self._lockfile.read_text().strip()
            raise LockError(
                f"Another AutoCoder instance is running (PID {pid}). "
                f"Remove {self._lockfile} if this is stale."
            )
        self._lockfile.parent.mkdir(parents=True, exist_ok=True)
        self._lockfile.write_text(str(os.getpid()))
        self._lock_held = True
        atexit.register(self.release_lock)

    def release_lock(self) -> None:
        if self._lock_held and self._lockfile.exists():
            self._lockfile.unlink()
            self._lock_held = False

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
        self._run("add", "-A")
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
        for prefix in ("feat/*", "ai/*"):
            result = self._run("branch", "--list", "--no-color", prefix)
            branches = [b.strip().lstrip("* ") for b in result.stdout.strip().split("\n") if b.strip()]
            for branch in branches:
                self._run("branch", "-D", branch, check=False)

    def create_worktree(self, path: str, branch: str, base: str | None = None) -> None:
        """Create a fresh worktree at `path`, on a new branch off `base` (default: main).

        Caller is responsible for `remove_worktree` on completion.
        """
        target = base or self.get_main_branch()
        # Force: drop any pre-existing branch with the same name
        self._run("branch", "-D", branch, check=False)
        self._run("worktree", "add", "-b", branch, path, target)

    def remove_worktree(self, path: str) -> None:
        """Force-remove a worktree at `path`. Safe to call multiple times."""
        self._run("worktree", "remove", "--force", path, check=False)

    def prune_worktrees(self) -> None:
        """Clean up worktree administrative records for removed directories."""
        self._run("worktree", "prune", check=False)
