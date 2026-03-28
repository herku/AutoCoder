import json
from unittest.mock import patch, MagicMock

from autocoder.issues import (
    fetch_issues,
    _parse_issue,
    _priority_sort,
    _build_prioritize_prompt,
    _parse_priority_response,
    analyze_and_prioritize,
    truncate_body,
)
from autocoder.types import Issue, Priority


def test_parse_issue():
    raw = {
        "number": 42,
        "title": "Fix bug",
        "body": "The bug is bad",
        "labels": [{"name": "P1"}, {"name": "bug"}],
        "url": "https://github.com/test/repo/issues/42",
    }
    issue = _parse_issue(raw, "P1")
    assert issue.number == 42
    assert issue.priority == Priority.P1
    assert "bug" in issue.labels


def test_priority_sort():
    issues = [
        Issue(3, "C", "", ["P2"], Priority.P2, ""),
        Issue(1, "A", "", ["P0"], Priority.P0, ""),
        Issue(2, "B", "", ["P1"], Priority.P1, ""),
        Issue(4, "D", "", ["P0"], Priority.P0, ""),
    ]
    sorted_issues = _priority_sort(issues)
    assert [i.number for i in sorted_issues] == [1, 4, 2, 3]


@patch("autocoder.issues.subprocess.run")
def test_fetch_issues(mock_run):
    mock_run.return_value = MagicMock(
        stdout=json.dumps([
            {
                "number": 1,
                "title": "Critical bug",
                "body": "Fix ASAP",
                "labels": [{"name": "P0"}],
                "url": "https://github.com/test/repo/issues/1",
            }
        ]),
        returncode=0,
    )
    issues = fetch_issues("/tmp/repo", ["P0"])
    assert len(issues) == 1
    assert issues[0].number == 1
    assert issues[0].priority == Priority.P0


# --- Truncation ---


def test_truncate_body_short():
    assert truncate_body("short body") == "short body"


def test_truncate_body_long():
    body = "paragraph one\n\n" + "x" * 5000
    result = truncate_body(body, 100)
    assert len(result) < 200
    assert "[...truncated]" in result


def test_truncate_body_preserves_paragraph():
    body = "first paragraph\n\nsecond paragraph\n\n" + "x" * 5000
    result = truncate_body(body, 50)
    assert "first paragraph" in result
    assert "[...truncated]" in result


# --- Auto-prioritization ---


def _make_issues():
    return [
        Issue(1, "Fix typo", "Simple typo in README", ["bug"], Priority.P3, ""),
        Issue(2, "Redesign auth", "Rewrite authentication system", ["enhancement"], Priority.P3, ""),
        Issue(3, "Add test", "Add unit test for parser", ["bug", "P1"], Priority.P1, ""),
    ]


def test_build_prioritize_prompt_contains_all_issues():
    issues = _make_issues()
    prompt = _build_prioritize_prompt(issues)
    assert "Issue #1: Fix typo" in prompt
    assert "Issue #2: Redesign auth" in prompt
    assert "Issue #3: Add test" in prompt
    assert "AUTOMABILITY" in prompt
    assert "JSON array" in prompt


def test_build_prioritize_prompt_truncates_body():
    issues = [Issue(1, "Big issue", "x" * 5000, [], Priority.P3, "")]
    prompt = _build_prioritize_prompt(issues)
    assert "x" * 1501 not in prompt
    assert "x" * 1000 in prompt


def test_parse_priority_response_valid():
    issues = _make_issues()
    raw = json.dumps([
        {"number": 1, "priority": "P0", "reason": "Simple typo"},
        {"number": 2, "priority": "P3", "reason": "Complex rewrite"},
        {"number": 3, "priority": "P1", "reason": "Straightforward test"},
    ])
    priorities, reasons = _parse_priority_response(raw, issues)
    assert priorities[1] == Priority.P0
    assert priorities[2] == Priority.P3
    assert priorities[3] == Priority.P1
    assert reasons[1] == "Simple typo"


def test_parse_priority_response_with_markdown_fences():
    issues = _make_issues()
    raw = '```json\n[{"number": 1, "priority": "P0", "reason": "fix"}]\n```'
    priorities, reasons = _parse_priority_response(raw, issues)
    assert priorities[1] == Priority.P0


def test_parse_priority_response_invalid_json():
    issues = _make_issues()
    priorities, reasons = _parse_priority_response("not json at all", issues)
    assert priorities == {}
    assert reasons == {}


def test_parse_priority_response_invalid_priority():
    issues = _make_issues()
    raw = json.dumps([{"number": 1, "priority": "URGENT", "reason": "bad"}])
    priorities, reasons = _parse_priority_response(raw, issues)
    assert 1 not in priorities


def test_parse_priority_response_unknown_issue_number():
    issues = _make_issues()
    raw = json.dumps([{"number": 999, "priority": "P0", "reason": "ghost"}])
    priorities, reasons = _parse_priority_response(raw, issues)
    assert 999 not in priorities


@patch("autocoder.issues.subprocess.run")
def test_analyze_and_prioritize(mock_run):
    issues = _make_issues()
    mock_run.return_value = MagicMock(
        stdout=json.dumps([
            {"number": 1, "priority": "P0", "reason": "Trivial typo"},
            {"number": 2, "priority": "P3", "reason": "Major rewrite"},
            {"number": 3, "priority": "P1", "reason": "Simple test"},
        ]),
        returncode=0,
    )
    result, reasons = analyze_and_prioritize(issues, "/tmp/repo", "sonnet")
    assert result[0].number == 1
    assert result[0].priority == Priority.P0
    assert result[1].number == 3
    assert result[1].priority == Priority.P1
    assert result[2].number == 2
    assert result[2].priority == Priority.P3
    assert reasons[1] == "Trivial typo"


@patch("autocoder.issues.subprocess.run")
def test_analyze_and_prioritize_fallback_on_failure(mock_run):
    issues = _make_issues()
    mock_run.return_value = MagicMock(stdout="", returncode=1)
    result, reasons = analyze_and_prioritize(issues, "/tmp/repo", "sonnet")
    assert result[0].priority == Priority.P1
    assert reasons == {}


def test_analyze_and_prioritize_empty():
    result, reasons = analyze_and_prioritize([], "/tmp/repo", "sonnet")
    assert result == []
    assert reasons == {}
