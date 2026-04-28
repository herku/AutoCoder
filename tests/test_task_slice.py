"""Tests for the task-slice heuristic and plan parser."""
from __future__ import annotations

from pathlib import Path

import pytest

from autocoder import task_slice
from autocoder.task_slice import (
    BODY_THRESHOLD_CHARS,
    CRITERIA_THRESHOLD,
    all_done,
    done_count,
    next_task,
    parse_plan,
    plan_path,
    should_task_slice,
    validate_plan,
)
from autocoder.types import Issue, Priority, RunConfig


def _cfg(**over):
    defaults = dict(
        repo_path="/tmp/r", labels=[],
        test_cmd=None, lint_cmd=None, integration_cmd=None,
        model="sonnet", plan_model="sonnet", review_model="sonnet",
        effort="max", triage_model="haiku",
        max_issues=10, max_analyze=0, max_turns=25,
        token_budget=1000, daily_cap=5000,
        docker=False, log_dir="./logs", dry_run=False,
        auto_prioritize=False, max_retries=3,
        protect_tests=False, test_patterns=[],
        auto_merge=False, plan_mode=False,
    )
    defaults.update(over)
    return RunConfig(**defaults)


def _issue(body: str, number: int = 42) -> Issue:
    return Issue(
        number=number, title="t", body=body, labels=[], priority=Priority.P2,
        url="u",
    )


# ---- Heuristic ----

def test_heuristic_triggers_on_enough_criteria():
    body = "\n".join(f"- [ ] criterion {i}" for i in range(CRITERIA_THRESHOLD))
    assert should_task_slice(_issue(body), _cfg()) is True


def test_heuristic_skips_below_criteria_threshold():
    body = "- [ ] only one thing\n"
    assert should_task_slice(_issue(body), _cfg()) is False


def test_heuristic_triggers_on_long_body():
    body = "x" * (BODY_THRESHOLD_CHARS + 1)
    assert should_task_slice(_issue(body), _cfg()) is True


def test_heuristic_skips_on_short_body_without_criteria():
    assert should_task_slice(_issue("short body"), _cfg()) is False


def test_explicit_true_overrides_heuristic():
    assert should_task_slice(_issue("short"), _cfg(task_slice=True)) is True


def test_explicit_false_overrides_heuristic():
    body = "\n".join(f"- [ ] criterion {i}" for i in range(CRITERIA_THRESHOLD))
    assert should_task_slice(_issue(body), _cfg(task_slice=False)) is False


# ---- Plan parser ----

def test_parse_plan_extracts_unchecked_and_checked_items():
    text = """# Plan

## Tasks
- [ ] First task
- [x] Already done
- [ ] Third task

## Notes
- not a checkbox
"""
    tasks = parse_plan(text)
    assert [t.text for t in tasks] == ["First task", "Already done", "Third task"]
    assert [t.done for t in tasks] == [False, True, False]
    assert [t.index for t in tasks] == [1, 2, 3]


def test_parse_plan_ignores_empty_checkboxes():
    text = "- [ ] \n- [ ] real task\n"
    tasks = parse_plan(text)
    assert len(tasks) == 1
    assert tasks[0].text == "real task"


def test_next_task_returns_first_unchecked():
    text = "- [x] a\n- [ ] b\n- [ ] c\n"
    nxt = next_task(parse_plan(text))
    assert nxt is not None
    assert nxt.text == "b"


def test_next_task_returns_none_when_all_done():
    text = "- [x] a\n- [x] b\n"
    assert next_task(parse_plan(text)) is None


def test_done_count_and_all_done():
    text = "- [x] a\n- [ ] b\n- [x] c\n"
    tasks = parse_plan(text)
    assert done_count(tasks) == 2
    assert all_done(tasks) is False

    all_text = "- [x] a\n- [x] b\n"
    assert all_done(parse_plan(all_text)) is True
    assert all_done([]) is False


def test_case_insensitive_x():
    text = "- [X] capital\n- [x] lower\n"
    tasks = parse_plan(text)
    assert all(t.done for t in tasks)


# ---- plan_path ----

def test_plan_path_is_under_autocoder_dir(tmp_path: Path):
    p = plan_path(str(tmp_path), 42)
    assert p.parent.name == ".autocoder"
    assert p.name == "plan-42.md"


# ---- validate_plan (placeholder lint) ----

def test_validate_plan_clean_returns_empty():
    plan = """# Plan
## Tasks
### Task 1: Add greeting
**Files:**
- Create: `src/greeter.py`
- Test: `tests/test_greeter.py`

- [ ] Step 1: Write the failing test
  ```python
  def test_greet():
      assert greet("ada") == "Hello, ada"
  ```
- [ ] Step 2: Run pytest, expect FAIL.
"""
    assert validate_plan(plan) == []


def test_validate_plan_flags_tbd():
    plan = "- [ ] Task 1: implement TBD foo"
    assert any("TBD" in v for v in validate_plan(plan))


def test_validate_plan_flags_todo_and_implement_later():
    plan = "TODO: do this thing\n- [ ] Step 2: implement later"
    violations = validate_plan(plan)
    assert any("TODO" in v for v in violations)
    assert any("implement later" in v.lower() for v in violations)


def test_validate_plan_flags_appropriate_error_handling():
    plan = "- [ ] Step 3: Add appropriate error handling around the file open"
    violations = validate_plan(plan)
    assert any("error handling" in v for v in violations)


def test_validate_plan_flags_similar_to_task_n():
    plan = "### Task 5: Refactor\n- [ ] Step 1: Similar to Task 2"
    assert any("similar to task n" in v.lower() for v in validate_plan(plan))


def test_validate_plan_flags_ellipsis_placeholder():
    plan = "- [ ] Step 1: do (...)"
    assert any("..." in v for v in validate_plan(plan))


def test_validate_plan_does_not_flag_handle_edge_cases_with_code_block():
    # 'handle edge cases' followed by a fenced code block on the same line is OK
    # (the executor has the actual handling). We're conservative: as long as
    # the line includes a code-block marker, no violation.
    plan = "- [ ] handle edge cases ```code```"
    assert validate_plan(plan) == []


def test_validate_plan_case_insensitive():
    plan = "- [ ] Step 1: tbd"
    assert any("TBD" in v for v in validate_plan(plan))
