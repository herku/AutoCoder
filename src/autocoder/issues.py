from __future__ import annotations

import json
import subprocess

from autocoder.types import Issue, Priority


PRIORITY_ORDER = [Priority.P0, Priority.P1, Priority.P2, Priority.P3]

ISSUE_BODY_MAX_CHARS = 4000


def fetch_issues(repo_path: str, labels: list[str], max_issues: int) -> list[Issue]:
    all_issues: list[Issue] = []
    seen: set[int] = set()

    for label in labels:
        if label not in [p.value for p in Priority]:
            # Non-priority label — fetch directly
            raw = _gh_fetch(repo_path, label, max_issues)
            for item in raw:
                if item["number"] not in seen:
                    seen.add(item["number"])
                    all_issues.append(_parse_issue(item, label))
            continue

        raw = _gh_fetch(repo_path, label, max_issues)
        for item in raw:
            if item["number"] not in seen:
                seen.add(item["number"])
                all_issues.append(_parse_issue(item, label))

    all_issues = _priority_sort(all_issues)
    return all_issues[:max_issues]


def _gh_fetch(repo_path: str, label: str, limit: int) -> list[dict]:
    result = subprocess.run(
        [
            "gh", "issue", "list",
            "--label", label,
            "--state", "open",
            "--json", "number,title,body,labels,url",
            "--limit", str(limit),
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


def summarize_issue_body(body: str, repo_path: str, model: str) -> str:
    if len(body) <= ISSUE_BODY_MAX_CHARS:
        return body

    result = subprocess.run(
        [
            "claude", "-p",
            "--model", model,
            "--output-format", "text",
            f"Summarize this GitHub issue body in under 2000 characters. "
            f"Preserve all technical details, file paths, error messages, and reproduction steps:\n\n{body}",
        ],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    # Fallback: hard truncate
    return body[:ISSUE_BODY_MAX_CHARS] + "\n\n[...truncated]"
