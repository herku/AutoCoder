import json
from unittest.mock import patch, MagicMock

from autocoder.agent import build_prompt, _parse_agent_output
from autocoder.types import Issue, Priority


def _make_issue(body="The widget crashes on save"):
    return Issue(
        number=42,
        title="Fix widget crash",
        body=body,
        labels=["P1", "bug"],
        priority=Priority.P1,
        url="https://github.com/test/repo/issues/42",
    )


def test_build_prompt_basic():
    prompt = build_prompt(_make_issue())
    assert "#42" in prompt
    assert "Fix widget crash" in prompt
    assert "widget crashes on save" in prompt
    assert "Do NOT modify existing test assertions" in prompt


def test_build_prompt_with_error_context():
    prompt = build_prompt(_make_issue(), error_context="TypeError: foo is not a function")
    assert "PREVIOUS ATTEMPT FAILED" in prompt
    assert "TypeError" in prompt
    assert "DIFFERENT approach" in prompt


def test_parse_agent_output_json():
    data = {
        "session_id": "sess-123",
        "result": "Fixed the issue",
        "is_error": False,
        "usage": {
            "input_tokens": 5000,
            "output_tokens": 2000,
            "cache_read_input_tokens": 1000,
        },
        "cost_usd": 0.05,
        "num_turns": 5,
    }
    result = _parse_agent_output(json.dumps(data), 3000, "sonnet")
    assert result.session_id == "sess-123"
    assert result.tokens_in == 5000
    assert result.tokens_out == 2000
    assert result.tokens_cached == 1000
    assert result.cost_usd == 0.05
    assert not result.is_error


def test_parse_agent_output_content_blocks():
    data = {
        "session_id": "sess-456",
        "result": [
            {"type": "text", "text": "I fixed the bug."},
            {"type": "text", "text": "Tests pass now."},
        ],
        "is_error": False,
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "cost_usd": 0.01,
        "num_turns": 2,
    }
    result = _parse_agent_output(json.dumps(data), 1000, "sonnet")
    assert "fixed the bug" in result.result_text
    assert "Tests pass" in result.result_text


def test_parse_agent_output_plain_text():
    result = _parse_agent_output("Just plain text output", 500, "sonnet")
    assert result.result_text == "Just plain text output"
    assert result.session_id == "unknown"
    assert result.tokens_in == 0
