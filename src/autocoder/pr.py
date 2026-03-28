from __future__ import annotations

import subprocess

from autocoder.types import Issue


def create_pr(repo_path: str, issue: Issue, branch: str, base: str = "main") -> str:
    # Push branch
    push = subprocess.run(
        ["git", "push", "-u", "origin", branch, "--force-with-lease"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if push.returncode != 0:
        raise RuntimeError(f"git push failed: {push.stderr.strip()}")

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
            "--base", base,
            "--title", title,
            "--body", body,
        ],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh pr create failed: {result.stderr.strip()}")
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


def get_pr_number(pr_url: str) -> int:
    return int(pr_url.rstrip("/").split("/")[-1])


def mark_ready(repo_path: str, pr_url: str) -> None:
    pr_num = get_pr_number(pr_url)
    subprocess.run(
        ["gh", "pr", "ready", str(pr_num)],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )


def merge_pr(repo_path: str, pr_url: str) -> bool:
    pr_num = get_pr_number(pr_url)
    result = subprocess.run(
        ["gh", "pr", "merge", str(pr_num), "--squash", "--delete-branch"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


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
