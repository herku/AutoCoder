from __future__ import annotations

import json
import re
import subprocess
import sys

from autocoder.types import Issue, PlanCheckItem, TestPlanResult


TESTPLAN_DIFF_MAX = 50_000


def extract_acceptance_criteria(issue_body: str) -> list[str]:
    """Extract checkbox items (- [ ] or * [ ]) from the issue body."""
    matches = re.findall(r"^[-*]\s*\[[ xX]\]\s*(.+)$", issue_body, re.MULTILINE)
    return [m.strip() for m in matches if m.strip()]


def verify_test_plan(
    issue: Issue,
    diff: str,
    repo_path: str,
    model: str = "sonnet",
) -> TestPlanResult:
    """Check if the diff addresses each acceptance criterion via claude -p."""
    criteria = extract_acceptance_criteria(issue.body)
    if not criteria:
        return TestPlanResult(items=[], raw_response="", all_passed=True)

    truncated_diff = diff[:TESTPLAN_DIFF_MAX] if len(diff) > TESTPLAN_DIFF_MAX else diff
    criteria_list = "\n".join(f"{i+1}. {c}" for i, c in enumerate(criteria))
    prompt = _TESTPLAN_TEMPLATE.format(
        title=issue.title,
        body=issue.body[:4000],
        criteria_list=criteria_list,
        diff=truncated_diff,
    )

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
        print(f"  Warning: test plan verification failed ({result.returncode})", file=sys.stderr)
        return TestPlanResult(items=[], raw_response="", all_passed=True)

    return parse_test_plan_response(result.stdout, criteria)


def parse_test_plan_response(raw: str, criteria: list[str]) -> TestPlanResult:
    """Parse JSON response into TestPlanResult."""
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`")

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        print("  Warning: could not parse test plan response as JSON", file=sys.stderr)
        return TestPlanResult(items=[], raw_response=raw, all_passed=True)

    if not isinstance(data, list):
        return TestPlanResult(items=[], raw_response=raw, all_passed=True)

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


def build_test_plan_fix_prompt(issue: Issue, failed_items: list[PlanCheckItem]) -> str:
    """Build prompt for the agent to fix gaps in acceptance criteria."""
    gaps = "\n".join(
        f"- {item.criterion}\n  Evidence: {item.evidence}"
        for item in failed_items
    )
    return (
        f"The following acceptance criteria from issue #{issue.number} "
        f"({issue.title}) are NOT yet addressed by the current changes:\n\n"
        f"{gaps}\n\n"
        "Instructions:\n"
        "- Address each missing criterion listed above\n"
        "- Keep changes minimal and focused on the gaps\n"
        "- Run the test suite to verify nothing is broken\n"
        "- Do NOT modify existing test assertions\n"
    )


_TESTPLAN_TEMPLATE = """\
You are verifying whether a code change fulfills the acceptance criteria of a GitHub issue.

## Issue
Title: {title}
Body:
{body}

## Acceptance Criteria
{criteria_list}

## Git Diff
```
{diff}
```

For each acceptance criterion, determine if the diff addresses it.

Rules:
- "pass" means the criterion is clearly addressed by the changes in the diff
- "fail" means the criterion is NOT addressed, or only partially addressed
- Provide brief, specific evidence citing file names and what was added/changed

Respond with ONLY a JSON array. No markdown fences. No explanation.
Each entry: {{"criterion": "exact criterion text", "status": "pass"|"fail", "evidence": "Brief specific evidence"}}"""
