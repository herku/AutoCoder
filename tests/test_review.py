import json

from autocoder.prompts import load
from autocoder.review import parse_review_response, build_fix_prompt
from autocoder.types import ReviewFinding


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
