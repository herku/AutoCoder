from __future__ import annotations

import subprocess
import time
from typing import Optional

from autocoder.types import CIResult, Issue, PlanCheckItem, VerifyResult, commit_prefix


def create_pr(
    repo_path: str,
    issue: Issue,
    branch: str,
    base: str = "main",
    *,
    summary: str = "",
    diff_stats: str = "",
    test_plan_items: Optional[list[PlanCheckItem]] = None,
    verify_results: Optional[list[VerifyResult]] = None,
) -> str:
    # Ensure base branch exists on remote (needed for new repos)
    check_base = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", base],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if not check_base.stdout.strip():
        push_base = subprocess.run(
            ["git", "push", "-u", "origin", base],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if push_base.returncode != 0:
            raise RuntimeError(f"git push base branch failed: {push_base.stderr.strip()}")

    # Push feature branch
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
    title = f"{commit_prefix(issue)}: {issue.title} (#{issue.number})"
    if len(title) > 70:
        title = title[:67] + "..."

    body = _build_pr_body(issue, summary, diff_stats, test_plan_items, verify_results)

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


def _build_pr_body(
    issue: Issue,
    summary: str,
    diff_stats: str,
    test_plan_items: Optional[list[PlanCheckItem]],
    verify_results: Optional[list[VerifyResult]],
) -> str:
    parts = [f"Fixes #{issue.number}\n"]

    if summary:
        # Truncate at sentence boundary
        text = summary[:500]
        if len(summary) > 500:
            last_period = text.rfind(".")
            if last_period > 200:
                text = text[:last_period + 1]
        parts.append(f"## Summary\n{text}\n")

    if diff_stats:
        parts.append(f"## Changes\n```\n{diff_stats}\n```\n")

    if test_plan_items:
        rows = []
        for item in test_plan_items:
            icon = "Pass" if item.status == "pass" else "Fail"
            criterion = item.criterion[:80] if len(item.criterion) > 80 else item.criterion
            evidence = item.evidence[:100] if len(item.evidence) > 100 else item.evidence
            rows.append(f"| {criterion} | {icon} | {evidence} |")
        table = "| Criterion | Status | Evidence |\n|-----------|--------|----------|\n" + "\n".join(rows)
        parts.append(f"## Test Plan\n{table}\n")

    if verify_results:
        lines = []
        for v in verify_results:
            icon = "passed" if v.passed else "FAILED"
            lines.append(f"- {v.stage}: {icon} ({v.duration_ms / 1000:.1f}s)")
        parts.append(f"## Verification\n" + "\n".join(lines) + "\n")

    parts.append("Automated fix by AutoCoder.")
    return "\n".join(parts)


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


def wait_for_new_checks(repo_path: str, commit_sha: str, timeout: int = 120) -> bool:
    """Poll until GitHub has registered check suites for commit_sha."""
    deadline = time.monotonic() + timeout
    delay = 5
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "gh", "api",
                f"repos/{{owner}}/{{repo}}/commits/{commit_sha}/check-suites",
                "--jq", ".total_count",
            ],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            if int(result.stdout.strip()) > 0:
                return True
        except (ValueError, AttributeError):
            pass
        time.sleep(min(delay, max(0, deadline - time.monotonic())))
        delay = min(delay + 5, 15)
    return False


def wait_for_ci(repo_path: str, pr_url: str, timeout: int) -> CIResult:
    """Wait for CI checks to complete via gh pr checks --watch."""
    pr_num = get_pr_number(pr_url)
    try:
        result = subprocess.run(
            ["gh", "pr", "checks", str(pr_num), "--watch", "--timeout", str(timeout)],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout + 30,
        )
    except subprocess.TimeoutExpired:
        return CIResult(passed=False, output="", timed_out=True)

    if result.returncode == 0:
        return CIResult(passed=True, output=result.stdout, timed_out=False)

    return CIResult(passed=False, output=result.stdout + result.stderr, timed_out=False)


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
