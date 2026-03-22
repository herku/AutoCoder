import json
from unittest.mock import patch, MagicMock

from autocoder.issues import fetch_issues, _parse_issue, _priority_sort
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
    issues = fetch_issues("/tmp/repo", ["P0"], 10)
    assert len(issues) == 1
    assert issues[0].number == 1
    assert issues[0].priority == Priority.P0
