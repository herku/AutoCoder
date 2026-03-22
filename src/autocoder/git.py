from __future__ import annotations

import atexit
import os
import subprocess
from pathlib import Path

from autocoder.types import LockError


class GitOps:
    def __init__(self, repo_path: str):
        self.repo_path = repo_path
        self._lockfile = Path(repo_path) / ".autocoder.lock"
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
        self._lockfile.write_text(str(os.getpid()))
        self._lock_held = True
        atexit.register(self.release_lock)

    def release_lock(self) -> None:
        if self._lock_held and self._lockfile.exists():
            self._lockfile.unlink()
            self._lock_held = False

    def assert_clean(self) -> None:
        result = self._run("status", "--porcelain")
        if result.stdout.strip():
            raise SystemExit(
                f"Error: Working directory is not clean:\n{result.stdout}\n"
                "Commit or stash changes before running AutoCoder."
            )

    def get_main_branch(self) -> str:
        for name in ("main", "master"):
            result = self._run("rev-parse", "--verify", name, check=False)
            if result.returncode == 0:
                return name
        raise SystemExit("Error: Could not find main or master branch")

    def save_checkpoint(self) -> str:
        result = self._run("rev-parse", "HEAD")
        return result.stdout.strip()

    def create_branch(self, issue_num: int) -> str:
        branch = f"ai/issue-{issue_num}"
        main = self.get_main_branch()
        # Delete existing branch if present
        self._run("branch", "-D", branch, check=False)
        self._run("checkout", main)
        self._run("checkout", "-b", branch)
        return branch

    def rollback(self, sha: str) -> None:
        self._run("reset", "--hard", sha)

    def checkout_main(self) -> None:
        main = self.get_main_branch()
        self._run("checkout", main)

    def delete_branch(self, branch: str) -> None:
        self._run("branch", "-D", branch, check=False)

    def commit_all(self, message: str) -> None:
        self._run("add", "-A")
        self._run("commit", "-m", message)

    def push_branch(self, branch: str) -> None:
        self._run("push", "-u", "origin", branch, "--force-with-lease")

    def diff_stats(self) -> str:
        main = self.get_main_branch()
        result = self._run("diff", "--stat", main, check=False)
        return result.stdout.strip()

    def diff_files(self) -> list[str]:
        main = self.get_main_branch()
        result = self._run("diff", "--name-only", main, check=False)
        return [f for f in result.stdout.strip().split("\n") if f]

    def cleanup_orphan_branches(self) -> None:
        result = self._run("branch", "--list", "--no-color", "ai/*")
        branches = [b.strip().lstrip("* ") for b in result.stdout.strip().split("\n") if b.strip()]
        for branch in branches:
            self._run("branch", "-D", branch, check=False)
