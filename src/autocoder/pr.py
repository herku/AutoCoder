from __future__ import annotations

import subprocess

from autocoder.types import Issue


def create_pr(repo_path: str, issue: Issue, branch: str) -> str:
    # Push branch
    subprocess.run(
        ["git", "push", "-u", "origin", branch, "--force-with-lease"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )

    # Create draft PR
    title = f"fix: {issue.title} (#{issue.number})"
    if len(title) > 70:
        title = title[:67] + "..."

    body = (
        f"Fixes #{issue.number}\n\n"
        f"Automated fix by AutoCoder.\n\n"
        f"**Issue:** {issue.title}\n"
        f"**Priority:** {issue.priority.value}"
    )

    result = subprocess.run(
        [
            "gh", "pr", "create",
            "--draft",
            "--title", title,
            "--body", body,
        ],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def label_failed(repo_path: str, issue_num: int) -> None:
    # Ensure the label exists
    subprocess.run(
        ["gh", "label", "create", "auto-fix-failed",
         "--description", "AutoCoder failed to resolve this issue",
         "--color", "D93F0B"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,  # Ignore if label already exists
    )

    subprocess.run(
        ["gh", "issue", "edit", str(issue_num), "--add-label", "auto-fix-failed"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )


def comment_failure(repo_path: str, issue_num: int, error: str) -> None:
    body = (
        "**AutoCoder failed to resolve this issue after maximum retries.**\n\n"
        f"```\n{error[:1000]}\n```\n\n"
        "This issue requires manual attention."
    )
    subprocess.run(
        ["gh", "issue", "comment", str(issue_num), "--body", body],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
