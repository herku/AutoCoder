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
