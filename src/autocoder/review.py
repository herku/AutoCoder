from __future__ import annotations

import json
import re
import subprocess
import sys

from autocoder.prompts import load
from autocoder.sandbox import SandboxConfig
from autocoder.types import AgentResult, MultiReviewResult, ReviewFinding, ReviewResult


REVIEW_DIFF_MAX = 50_000
REVIEW_MULTI_TIMEOUT = 900  # 15 minutes — 5 parallel sub-agents take time


def review_pr_diff(diff: str, repo_path: str, model: str = "sonnet") -> ReviewResult:
    """Run a code review on the given diff via claude -p."""
    truncated = diff[:REVIEW_DIFF_MAX] if len(diff) > REVIEW_DIFF_MAX else diff
    prompt = load("review", repo_path).format(diff=truncated)

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


def build_fix_prompt(findings: list[ReviewFinding], repo_path: str = "") -> str:
    """Build a prompt for the fix agent based on review findings."""
    issues_text = "\n".join(
        f"- [{f.severity.upper()}] {f.file}: {f.description}"
        for f in findings
    )
    return load("review_fix", repo_path or None).format(issues_text=issues_text)


CI_OUTPUT_MAX = 30_000


def build_ci_fix_prompt(ci_output: str, previous_attempts: str = "", repo_path: str = "") -> str:
    """Build a prompt for the fix agent based on CI failure output."""
    from autocoder.agent import _error_block

    truncated = ci_output[:CI_OUTPUT_MAX] if len(ci_output) > CI_OUTPUT_MAX else ci_output
    base = load("ci_fix", repo_path or None).format(ci_output=truncated)
    if previous_attempts:
        base += _error_block(
            previous_attempts,
            message="Previous CI fix attempt(s) did not resolve the issue.",
        )
    return base


BUILD_OUTPUT_MAX = 30_000


def build_build_fix_prompt(build_output: str, build_cmd: str = "", repo_path: str = "") -> str:
    """Build a prompt for the agent to fix a build failure."""
    truncated = build_output[:BUILD_OUTPUT_MAX] if len(build_output) > BUILD_OUTPUT_MAX else build_output
    return load("build_fix", repo_path or None).format(build_output=truncated, build_cmd=build_cmd or "unknown")


# ---------------------------------------------------------------------------
# External (second-opinion) reviewer
# ---------------------------------------------------------------------------


EXTERNAL_REVIEW_TIMEOUT = 180


def run_external_review(
    diff: str,
    cmd: list[str],
    repo_path: str,
    model_label: str = "external",
) -> tuple[ReviewResult, int]:
    """Run a second-opinion reviewer. The command receives the standard review
    prompt on stdin and must emit a JSON array of findings on stdout.

    Returns (ReviewResult, duration_ms). Failures are non-fatal — returns an
    empty result with a warning so the primary review still proceeds.
    """
    import time

    truncated = diff[:REVIEW_DIFF_MAX] if len(diff) > REVIEW_DIFF_MAX else diff
    prompt = load("review", repo_path).format(diff=truncated)
    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd, input=prompt, cwd=repo_path,
            capture_output=True, text=True, check=False,
            timeout=EXTERNAL_REVIEW_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        print(f"  Warning: external reviewer ({model_label}) failed: {e}", file=sys.stderr)
        return ReviewResult(findings=[], raw_response="", has_actionable_issues=False), int((time.monotonic() - start) * 1000)
    duration_ms = int((time.monotonic() - start) * 1000)

    if result.returncode != 0:
        print(f"  Warning: external reviewer ({model_label}) exited {result.returncode}", file=sys.stderr)
        return ReviewResult(findings=[], raw_response=result.stdout, has_actionable_issues=False), duration_ms

    return parse_review_response(result.stdout), duration_ms


def merge_reviews(primary: ReviewResult, external: ReviewResult) -> ReviewResult:
    """Union primary and external findings, deduping by (file, description[:80] lowercased)."""
    seen: set[tuple[str, str]] = set()
    merged: list[ReviewFinding] = []
    for finding in list(primary.findings) + list(external.findings):
        key = (finding.file, finding.description.lower()[:80])
        if key in seen:
            continue
        seen.add(key)
        merged.append(finding)
    return ReviewResult(
        findings=merged,
        raw_response=primary.raw_response,  # primary's raw response is the canonical one
        has_actionable_issues=bool(merged),
    )


# ---------------------------------------------------------------------------
# Multi-agent review (orchestrator spawns 5 parallel Task sub-agents)
# ---------------------------------------------------------------------------


def _format_external_findings(external: ReviewResult | None) -> str:
    if external is None or not external.findings:
        return "(none)"
    return "\n".join(
        f"- [{f.severity.upper()}] {f.file}: {f.description}"
        for f in external.findings
    )


def _parse_multi_signal(raw: str) -> tuple[bool, bool, str]:
    """Parse REVIEW_DONE / REVIEW_FIXED / REVIEW_FAILED signal from final line.

    Returns (cleaned, failed, summary).
    """
    text = (raw or "").strip()
    if not text:
        return False, True, "empty response"
    last = text.splitlines()[-1].strip()
    if last == "REVIEW_DONE" or last == "REVIEW_FIXED":
        return True, False, last
    if last.startswith("REVIEW_FAILED"):
        return False, True, last
    # No signal line — treat as ambiguous failure so caller doesn't auto-merge
    return False, True, f"no signal line (last: {last[:80]})"


def review_and_fix_multi(
    diff: str,
    repo_path: str,
    model: str,
    sandbox: SandboxConfig,
    budget_usd: float,
    external: ReviewResult | None = None,
) -> tuple[MultiReviewResult, AgentResult]:
    """Run the multi-agent orchestrator. It fixes issues in-session.

    Returns (outcome, agent_result) so callers can record telemetry.
    """
    from autocoder.agent import invoke_agent

    truncated = diff[:REVIEW_DIFF_MAX] if len(diff) > REVIEW_DIFF_MAX else diff
    prompt = load("review_multi", repo_path).format(
        diff=truncated,
        external_findings=_format_external_findings(external),
    )
    result = invoke_agent(
        prompt, repo_path, model, "max", budget_usd, sandbox,
        timeout=REVIEW_MULTI_TIMEOUT,
    )
    cleaned, failed, summary = _parse_multi_signal(result.result_text)
    return (
        MultiReviewResult(
            cleaned=cleaned, failed=failed, summary=summary, raw_response=result.result_text,
        ),
        result,
    )
