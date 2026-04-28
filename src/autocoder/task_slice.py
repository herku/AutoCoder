"""Task-sliced implementation — decompose big issues into fresh-context tasks.

Adopted from ralphex's Ralph loop: each task runs in a new Claude subprocess
to avoid context degradation on large/complex issues.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from autocoder.testplan import extract_acceptance_criteria
from autocoder.types import Issue, RunConfig, TaskItem

BODY_THRESHOLD_CHARS = 1500
CRITERIA_THRESHOLD = 3

# Match a checkbox line only within a single line — never let whitespace eat
# newlines (which would cause one task's `.+?` to greedily span the next).
_CHECKBOX_RE = re.compile(
    r"^[ \t]*-[ \t]*\[([ xX])\][ \t]+(\S[^\n]*?)[ \t]*$",
    re.MULTILINE,
)


def should_task_slice(issue: Issue, cfg: RunConfig) -> bool:
    """Decide whether to slice this issue into fresh-context tasks.

    Explicit `--task-slice` / `--no-task-slice` overrides the auto-heuristic.
    Otherwise: enable when the issue looks big enough to suffer context drift
    in a single session.
    """
    if cfg.task_slice is not None:
        return cfg.task_slice
    criteria = extract_acceptance_criteria(issue.body or "")
    if len(criteria) >= CRITERIA_THRESHOLD:
        return True
    if len(issue.body or "") > BODY_THRESHOLD_CHARS:
        return True
    return False


def plan_path(repo_path: str, issue_number: int) -> Path:
    """Per-issue task plan file path inside `.autocoder/`."""
    return Path(repo_path) / ".autocoder" / f"plan-{issue_number}.md"


def parse_plan(plan_text: str) -> list[TaskItem]:
    """Parse a plan markdown into TaskItem objects.

    Captures any line matching `- [ ] text` or `- [x] text`. Index is 1-based
    in source order, which is how the downstream executor refers to tasks.
    """
    tasks: list[TaskItem] = []
    for idx, match in enumerate(_CHECKBOX_RE.finditer(plan_text), start=1):
        done = match.group(1).lower() == "x"
        text = match.group(2).strip()
        if not text:
            continue
        tasks.append(TaskItem(index=idx, text=text, done=done))
    return tasks


def next_task(tasks: list[TaskItem]) -> Optional[TaskItem]:
    for t in tasks:
        if not t.done:
            return t
    return None


def done_count(tasks: list[TaskItem]) -> int:
    return sum(1 for t in tasks if t.done)


def all_done(tasks: list[TaskItem]) -> bool:
    return bool(tasks) and all(t.done for t in tasks)


# Plan-quality lint: forbidden phrases that signal an under-specified plan.
# Patterns are matched case-insensitively. Each pattern carries the rationale
# so a regenerated plan can cite specifically why the prior draft was rejected.
_PLACEHOLDER_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bTBD\b", "literal TBD — write the actual content the executor needs"),
    (r"\bTODO\b", "literal TODO — write the actual content"),
    (r"\bFIXME\b", "literal FIXME"),
    (r"\bimplement\s+later\b", "'implement later' — turn it into a concrete step now"),
    (r"\bfill\s+in\s+details?\b", "'fill in details' — fill them in"),
    (r"\bas\s+appropriate\b", "'as appropriate' — name the specific behaviour"),
    (
        r"\badd\s+appropriate\s+(error\s+handling|validation|guard\w*)\b",
        "'add appropriate error handling/validation' — show the actual handling code",
    ),
    (
        r"\bhandle\s+edge\s+cases\b(?![^\n]*```)",
        "'handle edge cases' without a code block — list the cases and the handling",
    ),
    (
        r"\bsimilar\s+to\s+task\s+\d+\b",
        "'similar to Task N' — repeat the actual content (the executor sees one task at a time)",
    ),
    (r"\bsame\s+as\s+above\b", "'same as above' — repeat the actual content"),
    (r"\bsee\s+task\s+\d+\b", "'see Task N' — repeat the actual content"),
    (r"<elided>", "'<elided>' placeholder"),
    (r"<omitted>", "'<omitted>' placeholder"),
    (r"\(\.\.\.\)", "'(...)' placeholder ellipsis"),
)


def validate_plan(plan_text: str) -> list[str]:
    """Return a list of plan-quality violations found in `plan_text`.

    Empty list means the plan passes. Each entry is a short reason describing
    the violation; callers are expected to feed the list back to the planner
    when regenerating.
    """
    violations: list[str] = []
    for pattern, reason in _PLACEHOLDER_PATTERNS:
        if re.search(pattern, plan_text, flags=re.IGNORECASE):
            violations.append(reason)
    return violations
