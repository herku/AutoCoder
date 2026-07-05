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
SPEC_REVIEW_TIMEOUT = 600   # 10 minutes — single sub-agent + 1 fix loop
SPEC_BUDGET_FRACTION = 0.4  # round 1 gets 40% of total budget; round 2 gets the rest
MIN_QUALITY_BUDGET = 0.20   # cents — abort round 2 if remaining is below this


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


# Models don't always stick to the critical/medium vocabulary the prompt asks
# for; anything clearly blocking maps to critical rather than being dropped.
_SEVERITY_MAP = {
    "critical": "critical",
    "blocker": "critical",
    "high": "critical",
    "major": "critical",
    "medium": "medium",
    "moderate": "medium",
}


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
        severity = _SEVERITY_MAP.get(entry.get("severity", "").lower())
        if severity is None:
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
LOG_HEAD_KEEP = 2_000


def _truncate_output(text: str, max_chars: int, head: int = LOG_HEAD_KEEP) -> str:
    """Truncate command/CI logs keeping the head AND the tail.

    Errors usually appear near the end of a log, but the first compile error
    matters too — so keep a small head and spend the rest of the budget on
    the tail, with an explicit marker for what was dropped.
    """
    if len(text) <= max_chars:
        return text
    head = min(head, max_chars // 4)
    tail = max_chars - head
    dropped = len(text) - head - tail
    return (
        text[:head]
        + f"\n... [{dropped} chars truncated] ...\n"
        + text[-tail:]
    )


def build_ci_fix_prompt(ci_output: str, previous_attempts: str = "", repo_path: str = "") -> str:
    """Build a prompt for the fix agent based on CI failure output."""
    from autocoder.agent import _error_block

    truncated = _truncate_output(ci_output, CI_OUTPUT_MAX)
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
    truncated = _truncate_output(build_output, BUILD_OUTPUT_MAX)
    return load("build_fix", repo_path or None).format(build_output=truncated, build_cmd=build_cmd or "unknown")


def build_ci_fix_arch_prompt(
    ci_output: str, previous_attempts: str, repo_path: str = "",
) -> str:
    """Build the analysis-only architectural critique prompt fired when CI-fix
    attempts have stalemated. The prompt forbids edits — the agent must return
    a recommendation only."""
    truncated = _truncate_output(ci_output, CI_OUTPUT_MAX)
    prior = previous_attempts.strip() or "(no prior attempts recorded)"
    return load("ci_fix_arch", repo_path or None).format(
        ci_output=truncated, previous_attempts=prior,
    )


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


def _parse_signal(raw: str, prefix: str) -> tuple[bool, bool, str]:
    """Parse a `<PREFIX>_DONE` / `<PREFIX>_FIXED` / `<PREFIX>_FAILED` signal from
    the final non-empty line.

    Returns (cleaned, failed, summary). `prefix` is e.g. "REVIEW", "SPEC",
    "QUALITY".
    """
    text = (raw or "").strip()
    if not text:
        return False, True, "empty response"
    last = text.splitlines()[-1].strip()
    done = f"{prefix}_DONE"
    fixed = f"{prefix}_FIXED"
    failed = f"{prefix}_FAILED"
    if last == done or last == fixed:
        return True, False, last
    if last.startswith(failed):
        return False, True, last
    return False, True, f"no {prefix.lower()} signal (last: {last[:80]})"


def _parse_multi_signal(raw: str) -> tuple[bool, bool, str]:
    """Backward-compatible parser for the legacy REVIEW_DONE/REVIEW_FIXED/REVIEW_FAILED
    signals — still used by the post-PR quality round, which emits QUALITY_*
    via the new prompt, but tests and callers may also pass legacy text."""
    cleaned, failed, summary = _parse_signal(raw, "QUALITY")
    if cleaned or failed and "no quality signal" not in summary:
        return cleaned, failed, summary
    # Fall back to the old signal namespace
    return _parse_signal(raw, "REVIEW")


def review_and_fix_multi(
    diff: str,
    repo_path: str,
    model: str,
    sandbox: SandboxConfig,
    budget_usd: float,
    external: ReviewResult | None = None,
    *,
    issue_body: str = "",
    telem: object | None = None,
    budget_tracker: object | None = None,
    spec_phase: object | None = None,
    quality_phase: object | None = None,
) -> tuple[MultiReviewResult, list[AgentResult]]:
    """Run two sequential review rounds: spec compliance first, then code quality.

    Round 1 (spec): single agent verifies the implementation matches the issue.
    Spawns ONE sub-agent and may attempt one fix pass. Short-circuits the whole
    review if the spec gap can't be closed.

    Round 2 (quality): five parallel agents review code quality, security,
    testing, simplification, documentation. Skipped if round 1 fails.

    Telemetry: each round calls `telem.record_phase(spec_phase | quality_phase, result)`
    when both `telem` and the corresponding phase are provided. This lets callers
    distinguish the spec vs quality cost buckets, and keeps the function reusable
    for both pre-verify and post-PR contexts (callers pass whichever Phase they
    want to attribute to).

    Budget: round 1 takes `SPEC_BUDGET_FRACTION` of `budget_usd`; round 2 takes
    the remainder (or `budget_usd - spent` if round 1 underran). Round 2 is
    skipped if remaining budget is below `MIN_QUALITY_BUDGET`.

    Returns (MultiReviewResult, [round_results]). `MultiReviewResult.failed` is
    True if either round failed; the summary names which round.
    """
    from autocoder.agent import invoke_agent

    truncated = diff[:REVIEW_DIFF_MAX] if len(diff) > REVIEW_DIFF_MAX else diff
    body = issue_body or "(no issue body provided to reviewer)"
    results: list[AgentResult] = []

    # ---- Round 1: spec compliance ----
    spec_budget = max(budget_usd * SPEC_BUDGET_FRACTION, 0.10)
    spec_prompt = load("review_spec_compliance", repo_path).format(
        diff=truncated, issue_body=body,
    )
    spec_result = invoke_agent(
        spec_prompt, repo_path, model, "max", spec_budget, sandbox,
        timeout=SPEC_REVIEW_TIMEOUT,
    )
    results.append(spec_result)
    if telem is not None and spec_phase is not None and budget_tracker is not None:
        budget_tracker.record(spec_result)  # type: ignore[attr-defined]
        telem.record_phase(spec_phase, spec_result)  # type: ignore[attr-defined]

    spec_cleaned, spec_failed, spec_summary = _parse_signal(spec_result.result_text, "SPEC")
    if spec_failed:
        return (
            MultiReviewResult(
                cleaned=False, failed=True,
                summary=f"spec:{spec_summary}",
                raw_response=spec_result.result_text,
            ),
            results,
        )

    # ---- Round 2: quality ----
    remaining = max(budget_usd - spec_result.cost_usd, 0.0)
    if remaining < MIN_QUALITY_BUDGET:
        return (
            MultiReviewResult(
                cleaned=spec_cleaned, failed=False,
                summary=f"spec:{spec_summary} | quality:skipped (budget exhausted)",
                raw_response=spec_result.result_text,
            ),
            results,
        )

    quality_prompt = load("review_quality", repo_path).format(
        diff=truncated,
        external_findings=_format_external_findings(external),
    )
    quality_result = invoke_agent(
        quality_prompt, repo_path, model, "max", remaining, sandbox,
        timeout=REVIEW_MULTI_TIMEOUT,
    )
    results.append(quality_result)
    if telem is not None and quality_phase is not None and budget_tracker is not None:
        budget_tracker.record(quality_result)  # type: ignore[attr-defined]
        telem.record_phase(quality_phase, quality_result)  # type: ignore[attr-defined]

    q_cleaned, q_failed, q_summary = _parse_signal(quality_result.result_text, "QUALITY")
    final_summary = f"spec:{spec_summary} | quality:{q_summary}"
    return (
        MultiReviewResult(
            cleaned=spec_cleaned and q_cleaned,
            failed=q_failed,
            summary=final_summary,
            raw_response=quality_result.result_text,
        ),
        results,
    )
