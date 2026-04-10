from __future__ import annotations

import subprocess
import sys

from autocoder.issues import fetch_sub_issues, parse_sub_issues
from autocoder.types import EpicResult, Issue, RateLimitError, is_epic

_MAX_EPIC_DEPTH = 2


def process_epic(epic, cfg, git, budget, log, timings, telem, *, depth=0):
    """Process an epic by implementing its sub-issues, then closing the epic."""
    tag = f"Epic #{epic.number}"
    sub_numbers = parse_sub_issues(epic.body)

    if not sub_numbers:
        print(f"  {tag}: no sub-issues found, skipping.")
        return EpicResult(
            epic_number=epic.number, sub_issues=[], succeeded=[],
            failed=[], skipped_closed=[], all_complete=True,
        )

    print(f"  {tag}: found {len(sub_numbers)} sub-issues: {', '.join(f'#{n}' for n in sub_numbers)}")
    open_issues, closed = fetch_sub_issues(cfg.repo_path, sub_numbers)
    if closed:
        print(f"  {tag}: already closed: {', '.join(f'#{n}' for n in closed)}")

    succeeded: list[int] = []
    failed: list[int] = []

    # Late import to avoid circular dependency
    from autocoder.loop import process_issue

    for i, sub in enumerate(open_issues, 1):
        if budget.daily_exhausted():
            print(f"  {tag}: daily cap reached, stopping sub-issue processing.")
            break

        if is_epic(sub):
            if depth >= _MAX_EPIC_DEPTH:
                print(f"  {tag}: nested epic #{sub.number} exceeds max depth {_MAX_EPIC_DEPTH}, skipping.")
                failed.append(sub.number)
                continue
            print(f"  {tag}: sub-issue #{sub.number} is an epic, processing recursively...")
            nested = process_epic(sub, cfg, git, budget, log, timings, telem, depth=depth + 1)
            if nested.all_complete:
                succeeded.append(sub.number)
            else:
                failed.append(sub.number)
            continue

        print(f"  {tag} [{i}/{len(open_issues)}]: processing #{sub.number}: {sub.title}")
        try:
            process_issue(sub, cfg, git, budget, log, timings, telem)
            succeeded.append(sub.number)
            update_epic_checkbox(cfg.repo_path, epic.number, sub.number)
        except RateLimitError:
            failed.append(sub.number)
            raise  # Propagate to stop all processing
        except Exception as e:
            print(f"  {tag}: sub-issue #{sub.number} failed: {str(e)[:200]}")
            failed.append(sub.number)

    all_complete = (
        len(succeeded) + len(closed) >= len(sub_numbers)
        and len(failed) == 0
    )

    result = EpicResult(
        epic_number=epic.number,
        sub_issues=sub_numbers,
        succeeded=succeeded,
        failed=failed,
        skipped_closed=closed,
        all_complete=all_complete,
    )

    if all_complete:
        close_epic(cfg.repo_path, epic, result)
    else:
        comment_epic_progress(cfg.repo_path, epic, result)

    return result


def update_epic_checkbox(repo_path: str, epic_number: int, sub_number: int) -> None:
    """Mark a sub-issue checkbox as complete in the epic body."""
    result = subprocess.run(
        ["gh", "issue", "view", str(epic_number), "--json", "body"],
        cwd=repo_path, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return

    import json
    body = json.loads(result.stdout).get("body", "")
    if not body:
        return

    import re
    # Replace - [ ] #N with - [x] #N (and URL variants)
    updated = re.sub(
        rf"^([-*]\s*)\[ \](\s*(?:https?://github\.com/[^/]+/[^/]+/issues/)?#?{sub_number}\b)",
        r"\1[x]\2",
        body,
        flags=re.MULTILINE,
    )

    if updated == body:
        return

    subprocess.run(
        ["gh", "issue", "edit", str(epic_number), "--body", updated],
        cwd=repo_path, capture_output=True, text=True, check=False,
    )


def close_epic(repo_path: str, epic: Issue, result: EpicResult) -> None:
    """Post summary comment and close the epic."""
    lines = ["**AutoCoder: Epic completed**\n", "All sub-issues have been resolved:\n"]
    for n in result.succeeded:
        lines.append(f"- #{n}: resolved via PR")
    for n in result.skipped_closed:
        lines.append(f"- #{n}: already closed")
    lines.append("\nClosing this epic.")
    body = "\n".join(lines)

    subprocess.run(
        ["gh", "issue", "comment", str(epic.number), "--body", body],
        cwd=repo_path, capture_output=True, text=True, check=False,
    )
    subprocess.run(
        ["gh", "issue", "close", str(epic.number)],
        cwd=repo_path, capture_output=True, text=True, check=False,
    )
    print(f"  Epic #{epic.number} closed.")


def comment_epic_progress(repo_path: str, epic: Issue, result: EpicResult) -> None:
    """Post a progress comment on a partially completed epic."""
    lines = ["**AutoCoder: Epic partially completed**\n"]
    if result.succeeded:
        lines.append(f"Completed: {', '.join(f'#{n}' for n in result.succeeded)}")
    if result.failed:
        lines.append(f"Failed: {', '.join(f'#{n}' for n in result.failed)}")
    if result.skipped_closed:
        lines.append(f"Already closed: {', '.join(f'#{n}' for n in result.skipped_closed)}")
    lines.append("\nThis epic remains open. Failed sub-issues need manual attention.")
    body = "\n".join(lines)

    subprocess.run(
        ["gh", "issue", "comment", str(epic.number), "--body", body],
        cwd=repo_path, capture_output=True, text=True, check=False,
    )
    print(f"  Epic #{epic.number} remains open ({len(result.failed)} sub-issues failed).")
