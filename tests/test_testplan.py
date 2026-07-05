import json

from autocoder.testplan import (
    extract_acceptance_criteria,
    parse_test_plan_response,
    build_test_plan_fix_prompt,
)
from autocoder.pr import _build_pr_body
from autocoder.types import Issue, Priority, PlanCheckItem, VerifyResult


def test_extract_criteria_with_checkboxes():
    body = "## Criteria\n- [ ] Add color palette\n- [x] Add typography\n- [ ] Add textures"
    result = extract_acceptance_criteria(body)
    assert len(result) == 3
    assert "Add color palette" in result[0]
    assert "Add typography" in result[1]


def test_extract_criteria_no_checkboxes():
    body = "This is a plain issue with no acceptance criteria."
    result = extract_acceptance_criteria(body)
    assert result == []


def test_extract_criteria_mixed():
    body = (
        "## Description\nSome text\n\n"
        "## Acceptance Criteria\n"
        "- [ ] First criterion\n"
        "- Regular bullet (not a checkbox)\n"
        "- [x] Second criterion\n"
        "Some more text\n"
        "* [ ] Third with asterisk\n"
    )
    result = extract_acceptance_criteria(body)
    assert len(result) == 3
    assert "First criterion" in result[0]
    assert "Second criterion" in result[1]
    assert "Third with asterisk" in result[2]


def test_parse_response_all_pass():
    criteria = ["Add colors", "Add fonts"]
    raw = json.dumps([
        {"criterion": "Add colors", "status": "pass", "evidence": "Theme.swift added"},
        {"criterion": "Add fonts", "status": "pass", "evidence": "Typography.swift added"},
    ])
    result = parse_test_plan_response(raw, criteria)
    assert result.all_passed
    assert len(result.items) == 2
    assert all(i.status == "pass" for i in result.items)


def test_parse_response_mixed():
    criteria = ["Add colors", "Add fonts"]
    raw = json.dumps([
        {"criterion": "Add colors", "status": "pass", "evidence": "Theme.swift added"},
        {"criterion": "Add fonts", "status": "fail", "evidence": "No typography file found"},
    ])
    result = parse_test_plan_response(raw, criteria)
    assert not result.all_passed
    assert result.items[0].status == "pass"
    assert result.items[1].status == "fail"


def test_parse_response_invalid_json_fails_closed():
    result = parse_test_plan_response("not json", ["Add colors"])
    # A garbled verifier response must never silently count as criteria met.
    assert not result.all_passed
    assert result.check_error
    assert result.items == []


def test_parse_response_non_list_json_fails_closed():
    result = parse_test_plan_response('{"criterion": "X"}', ["X"])
    assert not result.all_passed
    assert result.check_error


def test_verify_test_plan_nonzero_exit_fails_closed(monkeypatch):
    import subprocess as sp
    from autocoder import testplan as tp

    issue = Issue(1, "T", "- [ ] crit one", [], Priority.P2, "")
    monkeypatch.setattr(
        tp, "_invoke_verifier",
        lambda *a, **k: sp.CompletedProcess([], returncode=2, stdout="", stderr="boom"),
    )
    result = tp.verify_test_plan(issue, "diff", "/tmp")
    assert not result.all_passed
    assert "exited 2" in result.check_error


def test_verify_test_plan_retries_once_on_parse_failure(monkeypatch):
    import subprocess as sp
    from autocoder import testplan as tp

    issue = Issue(1, "T", "- [ ] crit one", [], Priority.P2, "")
    good = json.dumps([{"criterion": "crit one", "status": "pass", "evidence": "e"}])
    responses = iter(["Sure! Here is my analysis in prose.", good])
    calls = {"n": 0}

    def fake_invoke(prompt, repo_path, model):
        calls["n"] += 1
        return sp.CompletedProcess([], returncode=0, stdout=next(responses), stderr="")

    monkeypatch.setattr(tp, "_invoke_verifier", fake_invoke)
    result = tp.verify_test_plan(issue, "diff", "/tmp")
    assert calls["n"] == 2
    assert result.all_passed
    assert not result.check_error


def test_verify_test_plan_unparseable_after_retry_fails_closed(monkeypatch):
    import subprocess as sp
    from autocoder import testplan as tp

    issue = Issue(1, "T", "- [ ] crit one", [], Priority.P2, "")
    monkeypatch.setattr(
        tp, "_invoke_verifier",
        lambda *a, **k: sp.CompletedProcess([], returncode=0, stdout="still prose", stderr=""),
    )
    result = tp.verify_test_plan(issue, "diff", "/tmp")
    assert not result.all_passed
    assert result.check_error


def test_parse_response_markdown_fences():
    raw = '```json\n[{"criterion": "X", "status": "pass", "evidence": "Y"}]\n```'
    result = parse_test_plan_response(raw, ["X"])
    assert len(result.items) == 1
    assert result.items[0].status == "pass"


def test_build_fix_prompt():
    issue = Issue(42, "Fix widget", "body", [], Priority.P0, "")
    failed = [
        PlanCheckItem("Add color palette", "fail", "No Theme.swift found"),
        PlanCheckItem("Add typography", "fail", "Missing font definitions"),
    ]
    prompt = build_test_plan_fix_prompt(issue, failed)
    assert "#42" in prompt
    assert "Add color palette" in prompt
    assert "Add typography" in prompt
    assert "No Theme.swift" in prompt


def test_build_pr_body_with_plan():
    issue = Issue(42, "Fix widget", "body", [], Priority.P0, "")
    items = [
        PlanCheckItem("Add colors", "pass", "Theme.swift added"),
        PlanCheckItem("Add fonts", "fail", "Missing"),
    ]
    verify = [
        VerifyResult(True, "lint", 0, "", "", 1200),
        VerifyResult(True, "unit", 0, "", "", 3400),
    ]
    body = _build_pr_body(issue, "Fixed the widget.", "2 files changed", items, verify)
    assert "Fixes #42" in body
    assert "## Summary" in body
    assert "Fixed the widget" in body
    assert "## Changes" in body
    assert "2 files changed" in body
    assert "## Test Plan" in body
    assert "Add colors" in body
    assert "Pass" in body
    assert "Fail" in body
    assert "## Verification" in body
    assert "lint" in body
    assert "unit" in body


def test_build_pr_body_without_plan():
    issue = Issue(42, "Fix widget", "body", [], Priority.P0, "")
    body = _build_pr_body(issue, "Fixed it.", "", None, None)
    assert "Fixes #42" in body
    assert "## Summary" in body
    assert "Test Plan" not in body
    assert "Verification" not in body
    assert "Automated fix by AutoCoder" in body
