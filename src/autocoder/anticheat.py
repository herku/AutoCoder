from __future__ import annotations

import fnmatch
import os
import stat
import subprocess

from autocoder.types import AntiCheatViolation


def protect_test_files(repo_path: str, patterns: list[str]) -> list[str]:
    test_files = _find_test_files(repo_path, patterns)
    for path in test_files:
        current = os.stat(path).st_mode
        os.chmod(path, current & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    return test_files


def restore_test_files(paths: list[str]) -> None:
    for path in paths:
        if os.path.exists(path):
            current = os.stat(path).st_mode
            os.chmod(path, current | stat.S_IWUSR)


def audit_diff(repo_path: str, patterns: list[str]) -> None:
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    changed_files = [f for f in result.stdout.strip().split("\n") if f]

    violations = []
    for changed in changed_files:
        for pattern in patterns:
            if fnmatch.fnmatch(changed, pattern):
                violations.append(changed)
                break

    if violations:
        raise AntiCheatViolation(
            f"Agent modified test files: {', '.join(violations)}. "
            "This may indicate the agent is 'cheating' by modifying tests instead of fixing code."
        )


def _find_test_files(repo_path: str, patterns: list[str]) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    all_files = result.stdout.strip().split("\n")
    test_files = []
    for f in all_files:
        for pattern in patterns:
            if fnmatch.fnmatch(f, pattern):
                test_files.append(os.path.join(repo_path, f))
                break
    return test_files
