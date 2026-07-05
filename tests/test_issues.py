import json
from unittest.mock import patch, MagicMock

from autocoder.issues import (
    fetch_issues,
    _parse_issue,
    _priority_sort,
    _dependency_reorder,
    _build_prioritize_prompt,
    _parse_priority_response,
    _load_cache,
    _save_cache,
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
    priorities, reasons, deps = _parse_priority_response(raw, issues)
    assert priorities[1] == Priority.P0
    assert priorities[2] == Priority.P3
    assert priorities[3] == Priority.P1
    assert reasons[1] == "Simple typo"
    assert deps == {}


def test_parse_priority_response_with_blocked_by():
    issues = _make_issues()
    raw = json.dumps([
        {"number": 1, "priority": "P0", "reason": "Typo", "blocked_by": []},
        {"number": 2, "priority": "P3", "reason": "Rewrite", "blocked_by": [1, 3]},
        {"number": 3, "priority": "P1", "reason": "Test", "blocked_by": [1]},
    ])
    priorities, reasons, deps = _parse_priority_response(raw, issues)
    assert deps[2] == [1, 3]
    assert deps[3] == [1]
    assert 1 not in deps


def test_parse_priority_response_blocked_by_filters_invalid():
    issues = _make_issues()
    raw = json.dumps([
        {"number": 1, "priority": "P0", "reason": "fix", "blocked_by": [999, "bad", 2]},
    ])
    _, _, deps = _parse_priority_response(raw, issues)
    assert deps[1] == [2]


def test_parse_priority_response_with_markdown_fences():
    issues = _make_issues()
    raw = '```json\n[{"number": 1, "priority": "P0", "reason": "fix"}]\n```'
    priorities, reasons, _ = _parse_priority_response(raw, issues)
    assert priorities[1] == Priority.P0


def test_parse_priority_response_invalid_json():
    issues = _make_issues()
    priorities, reasons, deps = _parse_priority_response("not json at all", issues)
    assert priorities == {}
    assert reasons == {}
    assert deps == {}


def test_parse_priority_response_invalid_priority():
    issues = _make_issues()
    raw = json.dumps([{"number": 1, "priority": "URGENT", "reason": "bad"}])
    priorities, reasons, _ = _parse_priority_response(raw, issues)
    assert 1 not in priorities


def test_parse_priority_response_unknown_issue_number():
    issues = _make_issues()
    raw = json.dumps([{"number": 999, "priority": "P0", "reason": "ghost"}])
    priorities, reasons, _ = _parse_priority_response(raw, issues)
    assert 999 not in priorities


@patch("autocoder.issues.subprocess.run")
def test_analyze_and_prioritize(mock_run, tmp_path):
    issues = _make_issues()
    mock_run.return_value = MagicMock(
        stdout=json.dumps([
            {"number": 1, "priority": "P0", "reason": "Trivial typo", "blocked_by": []},
            {"number": 2, "priority": "P3", "reason": "Major rewrite", "blocked_by": []},
            {"number": 3, "priority": "P1", "reason": "Simple test", "blocked_by": []},
        ]),
        returncode=0,
    )
    result, reasons, deps = analyze_and_prioritize(issues, str(tmp_path), "sonnet")
    assert result[0].number == 1
    assert result[0].priority == Priority.P0
    assert result[1].number == 3
    assert result[1].priority == Priority.P1
    assert result[2].number == 2
    assert result[2].priority == Priority.P3
    assert reasons[1] == "Trivial typo"


@patch("autocoder.issues.subprocess.run")
def test_analyze_and_prioritize_with_dependencies(mock_run, tmp_path):
    issues = _make_issues()
    mock_run.return_value = MagicMock(
        stdout=json.dumps([
            {"number": 1, "priority": "P0", "reason": "Typo", "blocked_by": [3]},
            {"number": 2, "priority": "P3", "reason": "Rewrite", "blocked_by": []},
            {"number": 3, "priority": "P1", "reason": "Test", "blocked_by": []},
        ]),
        returncode=0,
    )
    result, reasons, deps = analyze_and_prioritize(issues, str(tmp_path), "sonnet")
    # #3 must come before #1 despite #1 being P0
    nums = [r.number for r in result]
    assert nums.index(3) < nums.index(1)
    assert deps[1] == [3]


@patch("autocoder.issues.subprocess.run")
def test_analyze_and_prioritize_fallback_on_failure(mock_run, tmp_path):
    issues = _make_issues()
    mock_run.return_value = MagicMock(stdout="", returncode=1)
    result, reasons, deps = analyze_and_prioritize(issues, str(tmp_path), "sonnet")
    assert result[0].priority == Priority.P1
    assert reasons == {}
    assert deps == {}


def test_analyze_and_prioritize_empty(tmp_path):
    result, reasons, deps = analyze_and_prioritize([], str(tmp_path), "sonnet")
    assert result == []
    assert reasons == {}
    assert deps == {}


# --- Dependency reorder ---


def test_dependency_reorder_simple():
    """Blocked issue moves after its blocker despite higher priority."""
    issues = [
        Issue(5, "Fast fix", "", [], Priority.P0, ""),
        Issue(8, "Foundation", "", [], Priority.P1, ""),
    ]
    deps = {5: [8]}
    result = _dependency_reorder(issues, deps)
    assert [i.number for i in result] == [8, 5]


def test_dependency_reorder_no_deps():
    """Without dependencies, order is unchanged."""
    issues = [
        Issue(1, "A", "", [], Priority.P0, ""),
        Issue(2, "B", "", [], Priority.P1, ""),
        Issue(3, "C", "", [], Priority.P2, ""),
    ]
    result = _dependency_reorder(issues, {})
    assert [i.number for i in result] == [1, 2, 3]


def test_dependency_reorder_chain():
    """Transitive chain: #1 blocked by #2, #2 blocked by #3."""
    issues = [
        Issue(1, "A", "", [], Priority.P0, ""),
        Issue(2, "B", "", [], Priority.P0, ""),
        Issue(3, "C", "", [], Priority.P0, ""),
    ]
    deps = {1: [2], 2: [3]}
    result = _dependency_reorder(issues, deps)
    assert [i.number for i in result] == [3, 2, 1]


def test_dependency_reorder_cycle():
    """Cycles are broken gracefully — all issues still appear."""
    issues = [
        Issue(1, "A", "", [], Priority.P0, ""),
        Issue(2, "B", "", [], Priority.P1, ""),
    ]
    deps = {1: [2], 2: [1]}
    result = _dependency_reorder(issues, deps)
    assert len(result) == 2
    assert {i.number for i in result} == {1, 2}


def test_dependency_reorder_partial():
    """Blocker not in batch is ignored."""
    issues = [
        Issue(1, "A", "", [], Priority.P0, ""),
        Issue(2, "B", "", [], Priority.P1, ""),
    ]
    deps = {1: [999]}  # 999 not in batch
    result = _dependency_reorder(issues, deps)
    assert [i.number for i in result] == [1, 2]


def test_dependency_reorder_preserves_priority_among_unrelated():
    """Unrelated issues keep their priority order."""
    issues = [
        Issue(1, "A", "", [], Priority.P0, ""),
        Issue(2, "B", "", [], Priority.P1, ""),
        Issue(3, "C", "", [], Priority.P2, ""),
        Issue(4, "D", "", [], Priority.P0, ""),
    ]
    # Only #3 is blocked by #4; #1 and #2 are independent
    deps = {3: [4]}
    result = _dependency_reorder(issues, deps)
    nums = [i.number for i in result]
    assert nums.index(4) < nums.index(3)
    assert nums.index(1) < nums.index(2)


# --- Prioritization cache ---


def test_save_load_roundtrip(tmp_path):
    issues = _make_issues()
    priorities = {1: Priority.P0, 2: Priority.P3, 3: Priority.P1}
    reasons = {1: "Simple", 2: "Complex", 3: "Medium"}
    deps = {2: [1, 3]}
    _save_cache(str(tmp_path), issues, priorities, reasons, deps)
    result = _load_cache(str(tmp_path), issues)
    assert result is not None
    loaded_pri, loaded_reasons, loaded_deps = result
    assert loaded_pri == priorities
    assert loaded_reasons == reasons
    assert loaded_deps == deps


def test_load_cache_subset_hit(tmp_path):
    """Removing issues (e.g. closed by AutoCoder) should still cache-hit."""
    all_issues = _make_issues()  # issues 1, 2, 3
    priorities = {1: Priority.P0, 2: Priority.P3, 3: Priority.P1}
    reasons = {1: "Simple", 2: "Complex", 3: "Medium"}
    deps = {2: [1, 3]}
    _save_cache(str(tmp_path), all_issues, priorities, reasons, deps)
    # Issue 1 was closed — only 2 and 3 remain
    subset = [Issue(2, "Redesign auth", "", [], Priority.P3, ""),
              Issue(3, "Add test", "", [], Priority.P1, "")]
    result = _load_cache(str(tmp_path), subset)
    assert result is not None
    loaded_pri, loaded_reasons, loaded_deps = result
    assert loaded_pri == {2: Priority.P3, 3: Priority.P1}
    assert loaded_reasons == {2: "Complex", 3: "Medium"}
    # Dep on issue 1 should be stripped since it's no longer present
    assert loaded_deps == {2: [3]}


def test_load_cache_partial_hit_new_issue(tmp_path):
    """A new issue not in cache returns partial results (cached issues only)."""
    issues_a = [Issue(1, "A", "", [], Priority.P0, ""), Issue(2, "B", "", [], Priority.P1, "")]
    _save_cache(str(tmp_path), issues_a, {1: Priority.P0, 2: Priority.P1}, {1: "reason"}, {})
    issues_b = [Issue(1, "A", "", [], Priority.P0, ""), Issue(4, "D", "", [], Priority.P1, "")]
    result = _load_cache(str(tmp_path), issues_b)
    assert result is not None
    priorities, reasons, deps = result
    assert priorities == {1: Priority.P0}  # only issue #1 cached, #4 is new
    assert reasons == {1: "reason"}
    assert deps == {}


def test_load_cache_missing_file(tmp_path):
    issues = _make_issues()
    assert _load_cache(str(tmp_path), issues) is None


@patch("autocoder.issues.subprocess.run")
def test_analyze_uses_cache(mock_run, tmp_path):
    issues = _make_issues()
    mock_run.return_value = MagicMock(
        stdout=json.dumps([
            {"number": 1, "priority": "P0", "reason": "Typo", "blocked_by": []},
            {"number": 2, "priority": "P3", "reason": "Rewrite", "blocked_by": []},
            {"number": 3, "priority": "P1", "reason": "Test", "blocked_by": []},
        ]),
        returncode=0,
    )
    analyze_and_prioritize(_make_issues(), str(tmp_path), "sonnet")
    assert mock_run.call_count == 1
    # Second call with same issues should use cache
    analyze_and_prioritize(_make_issues(), str(tmp_path), "sonnet")
    assert mock_run.call_count == 1


@patch("autocoder.issues.subprocess.run")
def test_force_bypasses_cache(mock_run, tmp_path):
    mock_run.return_value = MagicMock(
        stdout=json.dumps([
            {"number": 1, "priority": "P0", "reason": "Typo", "blocked_by": []},
            {"number": 2, "priority": "P3", "reason": "Rewrite", "blocked_by": []},
            {"number": 3, "priority": "P1", "reason": "Test", "blocked_by": []},
        ]),
        returncode=0,
    )
    analyze_and_prioritize(_make_issues(), str(tmp_path), "sonnet")
    assert mock_run.call_count == 1
    analyze_and_prioritize(_make_issues(), str(tmp_path), "sonnet", force=True)
    assert mock_run.call_count == 2


def test_fetch_issue_comments_parses_author_and_body():
    from autocoder.issues import fetch_issue_comments

    payload = json.dumps({"comments": [
        {"author": {"login": "alice"}, "body": "Use the v2 endpoint instead."},
        {"author": {"login": "bob"}, "body": "  "},
        {"author": None, "body": "drive-by note"},
    ]})
    proc = MagicMock(returncode=0, stdout=payload)
    with patch("autocoder.issues.subprocess.run", return_value=proc):
        comments = fetch_issue_comments("/tmp/x", 7)
    assert comments[0] == "alice: Use the v2 endpoint instead."
    # Blank bodies dropped; missing author tolerated.
    assert len(comments) == 2


def test_fetch_issue_comments_failures_return_empty():
    from autocoder.issues import fetch_issue_comments

    with patch("autocoder.issues.subprocess.run", return_value=MagicMock(returncode=1, stdout="")):
        assert fetch_issue_comments("/tmp/x", 7) == []
    with patch("autocoder.issues.subprocess.run", return_value=MagicMock(returncode=0, stdout="not json")):
        assert fetch_issue_comments("/tmp/x", 7) == []
