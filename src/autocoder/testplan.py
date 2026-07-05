from __future__ import annotations

import json
import re
import subprocess
import sys

from autocoder.prompts import load
from autocoder.types import Issue, PlanCheckItem, TestPlanResult


TESTPLAN_DIFF_MAX = 50_000


def extract_acceptance_criteria(issue_body: str) -> list[str]:
    """Extract checkbox items (- [ ] or * [ ]) from the issue body."""
    matches = re.findall(r"^[-*]\s*\[[ xX]\]\s*(.+)$", issue_body, re.MULTILINE)
    return [m.strip() for m in matches if m.strip()]


def _invoke_verifier(prompt: str, repo_path: str, model: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["claude", "-p", "--model", model, "--output-format", "text"],
        input=prompt,
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )


def verify_test_plan(
    issue: Issue,
    diff: str,
    repo_path: str,
    model: str = "sonnet",
) -> TestPlanResult:
    """Check if the diff addresses each acceptance criterion via claude -p.

    Fails CLOSED: a broken verifier (non-zero exit, unparseable JSON after one
    reformat retry) returns all_passed=False with check_error set, so callers
    can distinguish "criteria unmet" from "verifier broke" — but a garbled
    response can never silently count as criteria met.
    """
    criteria = extract_acceptance_criteria(issue.body)
    if not criteria:
        return TestPlanResult(items=[], raw_response="", all_passed=True)

    truncated_diff = diff[:TESTPLAN_DIFF_MAX] if len(diff) > TESTPLAN_DIFF_MAX else diff
    criteria_list = "\n".join(f"{i+1}. {c}" for i, c in enumerate(criteria))
    prompt = load("testplan", repo_path).format(
        title=issue.title,
        body=issue.body[:4000],
        criteria_list=criteria_list,
        diff=truncated_diff,
    )

    result = _invoke_verifier(prompt, repo_path, model)
    if result.returncode != 0:
        print(f"  Warning: test plan verification failed ({result.returncode})", file=sys.stderr)
        return TestPlanResult(
            items=[], raw_response=result.stdout + result.stderr,
            all_passed=False, check_error=f"verifier exited {result.returncode}",
        )

    parsed = parse_test_plan_response(result.stdout, criteria)
    if not parsed.check_error:
        return parsed

    # One reformat retry: the model produced prose/invalid JSON.
    retry_prompt = (
        prompt
        + "\n\nYour previous response was not valid JSON:\n"
        + result.stdout[:1000]
        + "\n\nRespond with ONLY the JSON array. No prose, no markdown fences."
    )
    retry = _invoke_verifier(retry_prompt, repo_path, model)
    if retry.returncode != 0:
        return TestPlanResult(
            items=[], raw_response=retry.stdout + retry.stderr,
            all_passed=False, check_error=f"verifier exited {retry.returncode} on retry",
        )
    return parse_test_plan_response(retry.stdout, criteria)


def parse_test_plan_response(raw: str, criteria: list[str]) -> TestPlanResult:
    """Parse JSON response into TestPlanResult."""
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`")

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        print("  Warning: could not parse test plan response as JSON", file=sys.stderr)
        return TestPlanResult(
            items=[], raw_response=raw, all_passed=False,
            check_error="unparseable verifier response",
        )

    if not isinstance(data, list):
        return TestPlanResult(
            items=[], raw_response=raw, all_passed=False,
            check_error="verifier response was not a JSON array",
        )

    items = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        status = entry.get("status", "fail").lower()
        if status not in ("pass", "fail"):
            status = "fail"
        items.append(PlanCheckItem(
            criterion=entry.get("criterion", ""),
            status=status,
            evidence=entry.get("evidence", ""),
        ))

    all_passed = all(item.status == "pass" for item in items) if items else True
    return TestPlanResult(items=items, raw_response=raw, all_passed=all_passed)


def build_test_plan_fix_prompt(issue: Issue, failed_items: list[PlanCheckItem], repo_path: str = "") -> str:
    """Build prompt for the agent to fix gaps in acceptance criteria."""
    gaps = "\n".join(
        f"- {item.criterion}\n  Evidence: {item.evidence}"
        for item in failed_items
    )
    return load("testplan_fix", repo_path or None).format(
        issue_number=issue.number,
        issue_title=issue.title,
        gaps=gaps,
    )
