import json
from unittest.mock import patch, MagicMock

import pytest

from autocoder import agent
from autocoder.agent import (
    build_brief_prompt,
    build_implement_prompt,
    build_prompt,
    _parse_agent_output,
    generate_implement_brief,
    invoke_agent,
    parse_status,
    set_rate_limit_wait,
)
from autocoder.sandbox import SandboxConfig
from autocoder.types import (
    AgentResult, ImplementerStatus, Issue, Priority, RateLimitError,
)


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


def test_format_commands_block_lists_configured_commands():
    from autocoder.agent import format_commands_block

    block = format_commands_block("npm run build", "npm test", "npm run lint")
    assert "`npm run lint`" in block
    assert "`npm test`" in block
    assert "`npm run build`" in block
    assert "Project commands" in block


def test_format_commands_block_empty_when_nothing_configured():
    from autocoder.agent import format_commands_block

    assert format_commands_block(None, None, None, None) == ""


def test_build_prompt_includes_commands_block():
    prompt = build_prompt(_make_issue(), commands_block="\n## Project commands\n- Tests: `uv run pytest`\n")
    assert "uv run pytest" in prompt


def test_build_prompt_includes_criteria_past_body_truncation():
    # Criteria sit past the 4000-char body cap; the implementer must still see them.
    body = ("x" * 4500) + "\n\n- [ ] hidden criterion alpha\n- [ ] hidden criterion beta\n"
    prompt = build_prompt(_make_issue(body=body))
    assert "Acceptance criteria (complete list)" in prompt
    assert "hidden criterion alpha" in prompt
    assert "hidden criterion beta" in prompt


def test_build_prompt_no_criteria_block_without_checkboxes():
    prompt = build_prompt(_make_issue())
    assert "Acceptance criteria (complete list)" not in prompt


def test_build_implement_prompt_includes_criteria_and_commands():
    body = "Fix it.\n- [ ] returns 200\n- [ ] logs the request"
    prompt = build_implement_prompt(
        _make_issue(body=body), "1. do the thing",
        commands_block="\n## Project commands\n- Build: `make build`\n",
    )
    assert "returns 200" in prompt
    assert "make build" in prompt


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


# ---------- pre-implement brief ----------


def test_build_brief_prompt_has_placeholders_filled():
    prompt = build_brief_prompt(_make_issue())
    assert "#42" in prompt
    assert "Fix widget crash" in prompt
    assert "widget crashes on save" in prompt
    # Agent markers should have been expanded by the loader
    assert "{{agent:architecture}}" not in prompt
    assert "{{agent:tests}}" not in prompt
    assert "{{agent:risks}}" not in prompt
    # Expanded content keywords
    assert "architecture" in prompt.lower()
    assert "test" in prompt.lower()
    assert "risk" in prompt.lower()


def test_build_prompt_with_brief_appends_block():
    prompt = build_prompt(_make_issue(), brief="- Touch foo.py\n- Add test for empty input")
    assert "Design brief from advisory agents:" in prompt
    assert "Touch foo.py" in prompt
    assert "Add test for empty input" in prompt


def test_build_prompt_without_brief_omits_block():
    prompt = build_prompt(_make_issue())
    assert "Design brief from advisory agents" not in prompt


def test_build_implement_prompt_with_brief():
    prompt = build_implement_prompt(
        _make_issue(), plan_text="plan goes here", brief="- Brief item one",
    )
    assert "plan goes here" in prompt
    assert "Design brief from advisory agents:" in prompt
    assert "Brief item one" in prompt


# ---------- status parsing ----------


def test_parse_status_done():
    text = "Did the work.\n\nSTATUS: DONE: tests pass 12/12"
    status, detail = parse_status(text)
    assert status is ImplementerStatus.DONE
    assert detail == "tests pass 12/12"


def test_parse_status_done_no_detail():
    text = "Did the work.\n\nSTATUS: DONE"
    status, detail = parse_status(text)
    assert status is ImplementerStatus.DONE
    assert detail is None


def test_parse_status_done_with_concerns():
    text = "Done.\nSTATUS: DONE_WITH_CONCERNS: flaky test in foo_test.py"
    status, detail = parse_status(text)
    assert status is ImplementerStatus.DONE_WITH_CONCERNS
    assert "flaky" in (detail or "")


def test_parse_status_blocked():
    text = "Tried 3 approaches.\nSTATUS: BLOCKED: API contract for /users is undocumented"
    status, detail = parse_status(text)
    assert status is ImplementerStatus.BLOCKED
    assert "undocumented" in (detail or "")


def test_parse_status_needs_context():
    text = "STATUS: NEEDS_CONTEXT: cannot find auth_provider.py"
    status, detail = parse_status(text)
    assert status is ImplementerStatus.NEEDS_CONTEXT
    assert "auth_provider" in (detail or "")


def test_parse_status_missing():
    status, detail = parse_status("Just some output without a status line.")
    assert status is None
    assert detail is None


def test_parse_status_multi_status_last_wins():
    text = (
        "STATUS: BLOCKED: initial guess\n"
        "Recovered after re-reading the file.\n"
        "STATUS: DONE: shipped"
    )
    status, detail = parse_status(text)
    assert status is ImplementerStatus.DONE
    assert detail == "shipped"


def test_parse_status_in_quoted_block():
    text = "> STATUS: DONE: from a quoted block still counts"
    status, detail = parse_status(text)
    assert status is ImplementerStatus.DONE


def test_parse_status_case_insensitive_token():
    # Implementer might lowercase by accident; the regex is case-insensitive on
    # the token, but we want to normalize to the canonical enum.
    text = "status: done: lower-case slip"
    status, _ = parse_status(text)
    assert status is ImplementerStatus.DONE


def test_parse_status_tolerates_angle_brackets():
    # A literal-minded model may copy the template's <TOKEN> form verbatim.
    status, detail = parse_status("STATUS: <DONE>: all tests pass")
    assert status is ImplementerStatus.DONE
    assert detail == "all tests pass"


def test_parse_status_angle_bracket_compound_token():
    # <DONE_WITH_CONCERNS> must not be misread as DONE.
    status, _ = parse_status("STATUS: <DONE_WITH_CONCERNS>: flaky test")
    assert status is ImplementerStatus.DONE_WITH_CONCERNS


def test_parse_agent_output_propagates_status():
    data = {
        "session_id": "sess-7",
        "result": "Implemented.\nSTATUS: BLOCKED: unclear API",
        "is_error": False,
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "cost_usd": 0.0,
        "num_turns": 1,
    }
    result = _parse_agent_output(json.dumps(data), 100, "sonnet")
    assert result.status is ImplementerStatus.BLOCKED
    assert result.status_detail and "unclear API" in result.status_detail


def test_generate_implement_brief_calls_invoke_agent():
    brief_text = "## Architecture\n- change foo.py\n## Tests to add\n- case 1"
    mock_result = AgentResult(
        session_id="brief", result_text=brief_text, is_error=False, duration_ms=100,
        tokens_in=1000, tokens_out=500, tokens_cached=0, cost_usd=0.02, num_turns=3, model="sonnet",
    )
    with patch("autocoder.agent.invoke_agent", return_value=mock_result) as mock_invoke:
        result = generate_implement_brief(
            _make_issue(), "/tmp", "sonnet", "max", 1.0, _sbx(),
        )
    assert result.result_text == brief_text
    mock_invoke.assert_called_once()
    # The prompt arg should include the issue title
    call_prompt = mock_invoke.call_args[0][0]
    assert "Fix widget crash" in call_prompt


def test_format_discussion_block_keeps_latest_comments():
    from autocoder.agent import format_discussion_block, DISCUSSION_MAX_COMMENTS

    comments = [f"user: comment {i}" for i in range(20)]
    block = format_discussion_block(comments)
    assert "comment 19" in block
    assert "comment 5" not in block  # older than the last 10
    assert format_discussion_block([]) == ""


def test_build_prompt_includes_discussion_block():
    prompt = build_prompt(
        _make_issue(),
        discussion_block="\n## Issue discussion\n- alice: use v2 endpoint\n",
    )
    assert "use v2 endpoint" in prompt
