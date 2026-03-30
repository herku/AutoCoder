from __future__ import annotations

import heapq
import json
import re
import subprocess
import sys

from autocoder.types import Issue, Priority


PRIORITY_ORDER = [Priority.P0, Priority.P1, Priority.P2, Priority.P3]

ISSUE_BODY_MAX_CHARS = 4000


def fetch_issues_by_number(repo_path: str, numbers: list[int]) -> list[Issue]:
    """Fetch specific issues by number via gh issue view."""
    issues: list[Issue] = []
    for num in numbers:
        result = subprocess.run(
            [
                "gh", "issue", "view", str(num),
                "--json", "number,title,body,labels,url",
            ],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print(f"  Warning: could not fetch issue #{num}: {result.stderr.strip()}", file=sys.stderr)
            continue
        raw = json.loads(result.stdout)
        issues.append(_parse_issue(raw, ""))
    return issues


def fetch_issues(repo_path: str, labels: list[str], limit: int = 0) -> list[Issue]:
    """Fetch open issues. If limit > 0, cap the result count."""
    all_issues: list[Issue] = []
    seen: set[int] = set()

    if not labels:
        raw = _gh_fetch_all(repo_path)
        for item in raw:
            if item["number"] not in seen:
                seen.add(item["number"])
                all_issues.append(_parse_issue(item, ""))
    else:
        for label in labels:
            raw = _gh_fetch(repo_path, label)
            for item in raw:
                if item["number"] not in seen:
                    seen.add(item["number"])
                    all_issues.append(_parse_issue(item, label))

    all_issues = _priority_sort(all_issues)
    return all_issues[:limit] if limit > 0 else all_issues


def _gh_fetch_all(repo_path: str) -> list[dict]:
    result = subprocess.run(
        [
            "gh", "issue", "list",
            "--state", "open",
            "--json", "number,title,body,labels,url",
            "--limit", "500",
        ],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else []


def _gh_fetch(repo_path: str, label: str) -> list[dict]:
    result = subprocess.run(
        [
            "gh", "issue", "list",
            "--label", label,
            "--state", "open",
            "--json", "number,title,body,labels,url",
            "--limit", "500",
        ],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else []


def _parse_issue(raw: dict, default_priority: str) -> Issue:
    label_names = [l["name"] for l in raw.get("labels", [])]
    priority = _extract_priority(label_names, default_priority)
    return Issue(
        number=raw["number"],
        title=raw["title"],
        body=raw.get("body", "") or "",
        labels=label_names,
        priority=priority,
        url=raw.get("url", ""),
    )


def _extract_priority(labels: list[str], default: str) -> Priority:
    for p in PRIORITY_ORDER:
        if p.value in labels:
            return p
    try:
        return Priority(default)
    except ValueError:
        return Priority.P3


def _priority_sort(issues: list[Issue]) -> list[Issue]:
    order = {p: i for i, p in enumerate(PRIORITY_ORDER)}
    return sorted(issues, key=lambda iss: (order.get(iss.priority, 99), iss.number))


def _dependency_reorder(
    issues: list[Issue],
    dependencies: dict[int, list[int]],
) -> list[Issue]:
    """Topological sort respecting dependencies while preserving priority order."""
    if not dependencies:
        return issues

    issue_map = {iss.number: iss for iss in issues}
    present = set(issue_map)
    order = {p: i for i, p in enumerate(PRIORITY_ORDER)}

    # Build in-degree map (only for edges where both ends are present)
    in_degree: dict[int, int] = {n: 0 for n in present}
    # reverse_adj: blocker -> list of issues it unblocks
    reverse_adj: dict[int, list[int]] = {n: [] for n in present}

    for num, blockers in dependencies.items():
        if num not in present:
            continue
        for b in blockers:
            if b in present:
                in_degree[num] += 1
                reverse_adj[b].append(num)

    # Kahn's algorithm with priority heap
    heap: list[tuple[int, int, int]] = []
    for num in present:
        if in_degree[num] == 0:
            iss = issue_map[num]
            heapq.heappush(heap, (order.get(iss.priority, 99), iss.number, num))

    result: list[Issue] = []
    while heap:
        _, _, num = heapq.heappop(heap)
        result.append(issue_map[num])
        for dependent in reverse_adj[num]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                iss = issue_map[dependent]
                heapq.heappush(heap, (order.get(iss.priority, 99), iss.number, dependent))

    # Handle cycles: remaining nodes with in_degree > 0
    if len(result) < len(issues):
        remaining = [n for n in present if n not in {iss.number for iss in result}]
        remaining_issues = sorted(
            [issue_map[n] for n in remaining],
            key=lambda iss: (order.get(iss.priority, 99), iss.number),
        )
        nums = ", ".join(f"#{n}" for n in remaining)
        print(f"  Warning: dependency cycle detected involving issues {nums}. Breaking cycle.", file=sys.stderr)
        result.extend(remaining_issues)

    return result


def truncate_body(body: str, max_chars: int = ISSUE_BODY_MAX_CHARS) -> str:
    """Truncate body preserving paragraph boundaries."""
    if len(body) <= max_chars:
        return body
    # Cut at last paragraph break before limit
    truncated = body[:max_chars]
    last_break = truncated.rfind("\n\n")
    if last_break > max_chars // 2:
        truncated = truncated[:last_break]
    return truncated + "\n\n[...truncated]"


# ---------------------------------------------------------------------------
# Auto-prioritization via claude -p
# ---------------------------------------------------------------------------

PRIORITIZE_BODY_MAX = 1500
PROMPT_CHAR_LIMIT = 400_000


def analyze_and_prioritize(
    issues: list[Issue],
    repo_path: str,
    triage_model: str = "sonnet",
) -> tuple[list[Issue], dict[int, str], dict[int, list[int]]]:
    """Send all issues to Claude for AI-based priority scoring."""
    if not issues:
        return issues, {}, {}

    prompt = _build_prioritize_prompt(issues)

    result = subprocess.run(
        ["claude", "-p", "--model", triage_model, "--output-format", "text"],
        input=prompt,
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )

    if result.returncode != 0:
        print(f"  Warning: auto-prioritize failed ({result.returncode}), using default priorities", file=sys.stderr)
        return _priority_sort(issues), {}, {}

    priorities, reasons, dependencies = _parse_priority_response(result.stdout, issues)

    if not priorities:
        return _priority_sort(issues), {}, {}

    for issue in issues:
        if issue.number in priorities:
            issue.priority = priorities[issue.number]

    sorted_issues = _priority_sort(issues)
    sorted_issues = _dependency_reorder(sorted_issues, dependencies)
    return sorted_issues, reasons, dependencies


def _build_prioritize_prompt(issues: list[Issue]) -> str:
    body_max = PRIORITIZE_BODY_MAX

    while body_max >= 200:
        formatted = _format_issues_for_prompt(issues, body_max)
        prompt = _PRIORITIZE_TEMPLATE.format(formatted_issues=formatted)
        if len(prompt) <= PROMPT_CHAR_LIMIT:
            return prompt
        body_max //= 2

    formatted = _format_issues_for_prompt(issues, 200)
    return _PRIORITIZE_TEMPLATE.format(formatted_issues=formatted)


def _format_issues_for_prompt(issues: list[Issue], body_max: int) -> str:
    parts = []
    for iss in issues:
        body = iss.body[:body_max] if len(iss.body) > body_max else iss.body
        labels_str = ", ".join(iss.labels) if iss.labels else "(none)"
        parts.append(
            f"### Issue #{iss.number}: {iss.title}\n"
            f"Labels: {labels_str}\n"
            f"Body:\n{body}"
        )
    return "\n\n".join(parts)


def _parse_priority_response(
    raw: str, issues: list[Issue]
) -> tuple[dict[int, Priority], dict[int, str], dict[int, list[int]]]:
    cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`")

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        print("  Warning: could not parse auto-prioritize response as JSON", file=sys.stderr)
        return {}, {}, {}

    if not isinstance(data, list):
        return {}, {}, {}

    valid_numbers = {iss.number for iss in issues}
    priorities: dict[int, Priority] = {}
    reasons: dict[int, str] = {}
    dependencies: dict[int, list[int]] = {}

    for entry in data:
        if not isinstance(entry, dict):
            continue
        num = entry.get("number")
        pri = entry.get("priority", "")
        reason = entry.get("reason", "")
        blocked_by = entry.get("blocked_by", [])

        if num not in valid_numbers:
            continue
        try:
            priorities[num] = Priority(pri)
        except ValueError:
            continue
        if reason:
            reasons[num] = reason
        if isinstance(blocked_by, list):
            valid_blockers = [b for b in blocked_by if isinstance(b, int) and b in valid_numbers]
            if valid_blockers:
                dependencies[num] = valid_blockers

    return priorities, reasons, dependencies


_PRIORITIZE_TEMPLATE = """\
You are a triage bot for an autonomous AI coding agent. Analyze these GitHub issues and assign each a priority for automated resolution.

Priority levels:
- P0: Trivial for AI. Simple bug fix, clear error message, small scope, well-defined acceptance criteria. Do first.
- P1: Straightforward. Clear requirements, moderate scope, AI can handle with some exploration.
- P2: Moderate complexity. May require understanding broader architecture, multiple files, or design decisions.
- P3: Hard/risky for AI. Architectural changes, ambiguous requirements, cross-cutting concerns, or depends on unresolved issues.

Scoring criteria (weight each equally):
1. AUTOMABILITY: Can an AI coding agent solve this autonomously? Clear reproduction steps and acceptance criteria = higher priority.
2. COMPLEXITY: Lines of code likely needed. Fewer = higher priority.
3. DEPENDENCIES: Does it depend on other issues in this batch? If yes, the dependency should be higher priority.
4. EXISTING LABELS: If the issue already has a priority label, treat it as a strong hint (but you may override if analysis disagrees).

Issues:
---
{formatted_issues}
---

Respond with ONLY a JSON array. No markdown fences. No explanation.
Include "blocked_by": a list of issue numbers from this batch that MUST be completed before this issue can start. Empty list if none.

Example:
[{{"number": 1, "priority": "P0", "reason": "Simple typo fix", "blocked_by": []}}, {{"number": 2, "priority": "P3", "reason": "Requires architectural redesign", "blocked_by": [1]}}]"""
