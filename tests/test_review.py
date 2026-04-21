import json
import subprocess
from unittest.mock import MagicMock, patch

from autocoder.prompts import load
from autocoder.review import (
    _format_external_findings,
    _parse_multi_signal,
    build_fix_prompt,
    merge_reviews,
    parse_review_response,
    review_and_fix_multi,
    run_external_review,
)
from autocoder.sandbox import SandboxConfig
from autocoder.types import AgentResult, ReviewFinding, ReviewResult


def test_parse_review_response_empty():
    result = parse_review_response("[]")
    assert not result.has_actionable_issues
    assert result.findings == []


def test_parse_review_response_with_findings():
    raw = json.dumps([
        {"severity": "critical", "file": "src/app.py", "description": "SQL injection"},
        {"severity": "medium", "file": "src/auth.py", "description": "Missing null check"},
    ])
    result = parse_review_response(raw)
    assert result.has_actionable_issues
    assert len(result.findings) == 2
    assert result.findings[0].severity == "critical"
    assert result.findings[0].file == "src/app.py"


def test_parse_review_response_filters_low():
    raw = json.dumps([
        {"severity": "critical", "file": "a.py", "description": "Bug"},
        {"severity": "low", "file": "b.py", "description": "Style issue"},
        {"severity": "info", "file": "c.py", "description": "Consider X"},
    ])
    result = parse_review_response(raw)
    assert len(result.findings) == 1
    assert result.findings[0].severity == "critical"


def test_parse_review_response_invalid_json():
    result = parse_review_response("not json")
    assert not result.has_actionable_issues
    assert result.findings == []


def test_parse_review_response_markdown_fences():
    raw = '```json\n[{"severity": "medium", "file": "x.py", "description": "Bug"}]\n```'
    result = parse_review_response(raw)
    assert len(result.findings) == 1


def test_build_fix_prompt():
    findings = [
        ReviewFinding("critical", "src/app.py", "SQL injection in query"),
        ReviewFinding("medium", "src/auth.py", "Missing null check"),
    ]
    prompt = build_fix_prompt(findings)
    assert "[CRITICAL]" in prompt
    assert "[MEDIUM]" in prompt
    assert "src/app.py" in prompt
    assert "SQL injection" in prompt
    assert "Fix ONLY" in prompt


def test_review_template_has_placeholders():
    template = load("review")
    assert "{diff}" in template
    assert "critical" in template.lower()


# ---------- multi-agent review ----------


def _ok_result(text: str) -> AgentResult:
    return AgentResult(
        session_id="s", result_text=text, is_error=False, duration_ms=1,
        tokens_in=0, tokens_out=0, tokens_cached=0, cost_usd=0.0, num_turns=1, model="sonnet",
    )


def _sbx() -> SandboxConfig:
    return SandboxConfig(allowed_tools=["Task", "Edit"], docker=False)


def test_parse_multi_signal_done():
    cleaned, failed, summary = _parse_multi_signal("Found nothing.\nREVIEW_DONE")
    assert cleaned and not failed
    assert "REVIEW_DONE" in summary


def test_parse_multi_signal_fixed():
    cleaned, failed, summary = _parse_multi_signal("Fixed two bugs.\nREVIEW_FIXED")
    assert cleaned and not failed


def test_parse_multi_signal_failed():
    cleaned, failed, summary = _parse_multi_signal("Can't fix\nREVIEW_FAILED: needs human")
    assert not cleaned
    assert failed
    assert "needs human" in summary


def test_parse_multi_signal_missing():
    cleaned, failed, summary = _parse_multi_signal("agent rambled without a signal")
    assert not cleaned
    assert failed


def test_parse_multi_signal_empty():
    cleaned, failed, _ = _parse_multi_signal("")
    assert not cleaned and failed


def test_format_external_findings_empty():
    assert _format_external_findings(None) == "(none)"
    assert _format_external_findings(ReviewResult([], "", False)) == "(none)"


def test_format_external_findings_with_items():
    findings = [ReviewFinding("critical", "x.py", "Bug"), ReviewFinding("medium", "y.py", "Leak")]
    text = _format_external_findings(ReviewResult(findings, "", True))
    assert "[CRITICAL] x.py: Bug" in text
    assert "[MEDIUM] y.py: Leak" in text


def test_review_and_fix_multi_done(tmp_path):
    (tmp_path / ".git").mkdir()  # satisfy anything that checks; not used by mock
    with patch("autocoder.agent.invoke_agent", return_value=_ok_result("all good\nREVIEW_DONE")):
        outcome, result = review_and_fix_multi(
            "diff text", str(tmp_path), "sonnet", _sbx(), 2.00,
        )
    assert outcome.cleaned
    assert not outcome.failed
    assert result.result_text.endswith("REVIEW_DONE")


def test_review_and_fix_multi_failed_signal(tmp_path):
    with patch("autocoder.agent.invoke_agent", return_value=_ok_result("tried\nREVIEW_FAILED: human needed")):
        outcome, _ = review_and_fix_multi(
            "diff text", str(tmp_path), "sonnet", _sbx(), 2.00,
        )
    assert outcome.failed
    assert "human needed" in outcome.summary


# ---------- external reviewer + merge ----------


def _cp(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_run_external_review_empty_findings(tmp_path):
    with patch("autocoder.review.subprocess.run", return_value=_cp("[]")):
        result, dur = run_external_review("diff", ["echo"], str(tmp_path))
    assert not result.has_actionable_issues
    assert result.findings == []
    assert dur >= 0


def test_run_external_review_with_findings(tmp_path):
    stdout = json.dumps([{"severity": "critical", "file": "x.py", "description": "Fake"}])
    with patch("autocoder.review.subprocess.run", return_value=_cp(stdout)):
        result, _ = run_external_review("diff", ["echo"], str(tmp_path))
    assert len(result.findings) == 1
    assert result.findings[0].file == "x.py"


def test_run_external_review_nonzero_exit_is_nonfatal(tmp_path):
    with patch("autocoder.review.subprocess.run", return_value=_cp("garbage", returncode=1)):
        result, _ = run_external_review("diff", ["echo"], str(tmp_path))
    assert not result.has_actionable_issues


def test_run_external_review_timeout_is_nonfatal(tmp_path):
    with patch("autocoder.review.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=[], timeout=1)):
        result, _ = run_external_review("diff", ["echo"], str(tmp_path))
    assert not result.has_actionable_issues
    assert result.findings == []


def test_run_external_review_missing_binary_is_nonfatal(tmp_path):
    with patch("autocoder.review.subprocess.run", side_effect=FileNotFoundError("no such")):
        result, _ = run_external_review("diff", ["nope"], str(tmp_path))
    assert not result.has_actionable_issues


def test_merge_reviews_unique_both_sides():
    primary = ReviewResult(
        findings=[ReviewFinding("critical", "a.py", "Alpha bug")],
        raw_response="p", has_actionable_issues=True,
    )
    external = ReviewResult(
        findings=[ReviewFinding("medium", "b.py", "Beta bug")],
        raw_response="e", has_actionable_issues=True,
    )
    merged = merge_reviews(primary, external)
    assert len(merged.findings) == 2
    assert merged.has_actionable_issues


def test_merge_reviews_dedupe_by_file_and_description_prefix():
    # Same file, descriptions that share the first 80 chars when lowercased
    desc = "Unchecked return from write() at line 42; caller assumes success on partial write"
    primary = ReviewResult(
        findings=[ReviewFinding("critical", "a.py", desc + " — trust me")],
        raw_response="p", has_actionable_issues=True,
    )
    external = ReviewResult(
        findings=[ReviewFinding("medium", "a.py", desc + " (redundant finding)")],
        raw_response="e", has_actionable_issues=True,
    )
    merged = merge_reviews(primary, external)
    assert len(merged.findings) == 1
    assert merged.findings[0].severity == "critical"  # primary kept


def test_merge_reviews_empty_sides():
    empty = ReviewResult(findings=[], raw_response="", has_actionable_issues=False)
    assert merge_reviews(empty, empty).findings == []


def test_merge_reviews_only_external():
    empty = ReviewResult(findings=[], raw_response="", has_actionable_issues=False)
    external = ReviewResult(
        findings=[ReviewFinding("critical", "x.py", "Bug")],
        raw_response="", has_actionable_issues=True,
    )
    merged = merge_reviews(empty, external)
    assert len(merged.findings) == 1
    assert merged.has_actionable_issues
