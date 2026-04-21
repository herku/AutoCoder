from __future__ import annotations

import json
import re
import subprocess
import sys

from autocoder.prompts import load
from autocoder.types import ReviewFinding, ReviewResult


REVIEW_DIFF_MAX = 50_000


def review_pr_diff(diff: str, repo_path: str, model: str = "sonnet") -> ReviewResult:
    """Run a code review on the given diff via claude -p."""
    truncated = diff[:REVIEW_DIFF_MAX] if len(diff) > REVIEW_DIFF_MAX else diff
    prompt = load("review").format(diff=truncated)

    result = subprocess.run(
        ["claude", "-p", "--model", model, "--output-format", "text"],
        input=prompt,
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )

    if result.returncode != 0:
        print(f"  Warning: review failed ({result.returncode})", file=sys.stderr)
        return ReviewResult(findings=[], raw_response="", has_actionable_issues=False)

    return parse_review_response(result.stdout)


def parse_review_response(raw: str) -> ReviewResult:
    """Parse the review JSON response into a ReviewResult."""
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`")

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        print("  Warning: could not parse review response as JSON", file=sys.stderr)
        return ReviewResult(findings=[], raw_response=raw, has_actionable_issues=False)

    if not isinstance(data, list):
        return ReviewResult(findings=[], raw_response=raw, has_actionable_issues=False)

    findings = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        severity = entry.get("severity", "").lower()
        if severity not in ("critical", "medium"):
            continue
        findings.append(ReviewFinding(
            severity=severity,
            file=entry.get("file", ""),
            description=entry.get("description", ""),
        ))

    return ReviewResult(
        findings=findings,
        raw_response=raw,
        has_actionable_issues=len(findings) > 0,
    )


def build_fix_prompt(findings: list[ReviewFinding]) -> str:
    """Build a prompt for the fix agent based on review findings."""
    issues_text = "\n".join(
        f"- [{f.severity.upper()}] {f.file}: {f.description}"
        for f in findings
    )
    return load("review_fix").format(issues_text=issues_text)


CI_OUTPUT_MAX = 30_000


def build_ci_fix_prompt(ci_output: str, previous_attempts: str = "") -> str:
    """Build a prompt for the fix agent based on CI failure output."""
    from autocoder.agent import _error_block

    truncated = ci_output[:CI_OUTPUT_MAX] if len(ci_output) > CI_OUTPUT_MAX else ci_output
    base = load("ci_fix").format(ci_output=truncated)
    if previous_attempts:
        base += _error_block(
            previous_attempts,
            message="Previous CI fix attempt(s) did not resolve the issue.",
        )
    return base


BUILD_OUTPUT_MAX = 30_000


def build_build_fix_prompt(build_output: str, build_cmd: str = "") -> str:
    """Build a prompt for the agent to fix a build failure."""
    truncated = build_output[:BUILD_OUTPUT_MAX] if len(build_output) > BUILD_OUTPUT_MAX else build_output
    return load("build_fix").format(build_output=truncated, build_cmd=build_cmd or "unknown")
