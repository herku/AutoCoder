from __future__ import annotations

import json
import re
import subprocess
import sys

from autocoder.types import ReviewFinding, ReviewResult


REVIEW_DIFF_MAX = 50_000


def review_pr_diff(diff: str, repo_path: str, model: str = "sonnet") -> ReviewResult:
    """Run a code review on the given diff via claude -p."""
    truncated = diff[:REVIEW_DIFF_MAX] if len(diff) > REVIEW_DIFF_MAX else diff
    prompt = _REVIEW_TEMPLATE.format(diff=truncated)

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
    return (
        "A code review found the following issues in the current changes. "
        "Fix each one:\n\n"
        f"{issues_text}\n\n"
        "Instructions:\n"
        "- Fix ONLY the listed issues, do not refactor unrelated code\n"
        "- Keep changes minimal\n"
        "- Run tests after making changes to verify nothing is broken\n"
        "- Do NOT modify existing test assertions\n"
    )


_REVIEW_TEMPLATE = """\
You are a code reviewer for an automated PR. Review the following git diff for critical and medium severity issues only.

Focus on:
- Bugs: logic errors, off-by-one, null/undefined access, race conditions
- Security: injection, exposed secrets, unsafe deserialization, path traversal
- Data loss: missing error handling that could corrupt state
- API contract: breaking changes to public interfaces

Do NOT report:
- Style/formatting issues
- Minor naming suggestions
- "Consider using X" recommendations
- Low severity or informational findings

Git diff:
```
{diff}
```

Respond with ONLY a JSON array. No markdown fences. No explanation.
Each finding: {{"severity": "critical"|"medium", "file": "path/to/file", "description": "Brief actionable description"}}
If no critical or medium issues found, respond with: []"""
