import json
from unittest.mock import patch, MagicMock

import pytest

from autocoder import agent
from autocoder.agent import build_prompt, _parse_agent_output, invoke_agent, set_rate_limit_wait
from autocoder.sandbox import SandboxConfig
from autocoder.types import AgentResult, Issue, Priority, RateLimitError


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


# ---------- wait-on-rate-limit ----------


@pytest.fixture
def _reset_wait():
    set_rate_limit_wait(None)
    yield
    set_rate_limit_wait(None)


def _ok_result() -> AgentResult:
    return AgentResult(
        session_id="s", result_text="", is_error=False, duration_ms=1,
        tokens_in=0, tokens_out=0, tokens_cached=0, cost_usd=0.0, num_turns=1, model="sonnet",
    )


def _sbx() -> SandboxConfig:
    return SandboxConfig(allowed_tools=["Read"], docker=False)


def test_invoke_agent_no_wait_raises_rate_limit(_reset_wait):
    with patch.object(agent, "_invoke_once", side_effect=RateLimitError("hit")):
        with pytest.raises(RateLimitError):
            invoke_agent("prompt", "/tmp", "sonnet", "max", 1.0, _sbx())


def test_invoke_agent_wait_retries_once_then_succeeds(_reset_wait):
    set_rate_limit_wait(1)
    calls = [RateLimitError("hit"), _ok_result()]
    with patch.object(agent, "_invoke_once", side_effect=calls), \
         patch.object(agent.time, "sleep") as sleep:
        result = invoke_agent("prompt", "/tmp", "sonnet", "max", 1.0, _sbx())
    assert result.session_id == "s"
    sleep.assert_called_once_with(1)


def test_invoke_agent_wait_caps_at_three_retries(_reset_wait):
    set_rate_limit_wait(1)
    with patch.object(agent, "_invoke_once", side_effect=RateLimitError("hit")), \
         patch.object(agent.time, "sleep") as sleep:
        with pytest.raises(RateLimitError):
            invoke_agent("prompt", "/tmp", "sonnet", "max", 1.0, _sbx())
    # 3 sleeps = retry #1, #2, #3; 4th attempt raises out without sleeping
    assert sleep.call_count == 3
