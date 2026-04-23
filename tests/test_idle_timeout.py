"""Tests for the idle + session watchdog in agent._run_with_watchdog."""
from __future__ import annotations

import subprocess
import time

import pytest

from autocoder import agent
from autocoder.agent import _run_with_watchdog, set_timeouts
from autocoder.types import IdleTimeoutError


@pytest.fixture(autouse=True)
def _reset_timeouts():
    set_timeouts(None, None)
    yield
    set_timeouts(None, None)


def test_no_timeouts_configured_runs_to_completion(tmp_path):
    rc, out, err = _run_with_watchdog(
        ["sh", "-c", "echo hello; echo stderr >&2"],
        prompt="",
        repo_path=str(tmp_path),
        wall_timeout=5,
        idle_seconds=None,
        session_seconds=None,
    )
    assert rc == 0
    assert "hello" in out
    assert "stderr" in err


def test_prompt_is_piped_via_stdin(tmp_path):
    rc, out, _ = _run_with_watchdog(
        ["cat"],
        prompt="the-prompt-body\n",
        repo_path=str(tmp_path),
        wall_timeout=5,
        idle_seconds=None,
        session_seconds=None,
    )
    assert rc == 0
    assert "the-prompt-body" in out


def test_idle_timeout_kills_silent_process(tmp_path):
    # Emits nothing for 5s; idle limit is 1s.
    start = time.monotonic()
    with pytest.raises(IdleTimeoutError) as exc:
        _run_with_watchdog(
            ["sh", "-c", "sleep 5"],
            prompt="",
            repo_path=str(tmp_path),
            wall_timeout=30,
            idle_seconds=1,
            session_seconds=None,
        )
    elapsed = time.monotonic() - start
    assert "idle" in str(exc.value).lower()
    # Must kill well before the 5s sleep finishes.
    assert elapsed < 4.0


def test_streaming_output_resets_idle_counter(tmp_path):
    # Emits a line every 0.3s for ~1.5s. Idle limit 1s should NOT fire.
    rc, out, _ = _run_with_watchdog(
        ["sh", "-c", "for i in 1 2 3 4 5; do echo $i; sleep 0.3; done"],
        prompt="",
        repo_path=str(tmp_path),
        wall_timeout=10,
        idle_seconds=1,
        session_seconds=None,
    )
    assert rc == 0
    assert "1" in out and "5" in out


def test_session_timeout_kills_long_process(tmp_path):
    start = time.monotonic()
    with pytest.raises(IdleTimeoutError) as exc:
        _run_with_watchdog(
            ["sh", "-c", "for i in $(seq 1 20); do echo $i; sleep 0.2; done"],
            prompt="",
            repo_path=str(tmp_path),
            wall_timeout=30,
            idle_seconds=None,
            session_seconds=1,
        )
    elapsed = time.monotonic() - start
    assert "session" in str(exc.value).lower()
    assert elapsed < 4.0


def test_wall_timeout_still_raises_timeout_expired(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        _run_with_watchdog(
            ["sh", "-c", "sleep 5"],
            prompt="",
            repo_path=str(tmp_path),
            wall_timeout=1,
            idle_seconds=None,
            session_seconds=None,
        )


def test_set_timeouts_updates_module_state():
    set_timeouts(30, 120)
    assert agent._idle_timeout_seconds == 30
    assert agent._session_timeout_seconds == 120
    set_timeouts(None, None)
    assert agent._idle_timeout_seconds is None
    assert agent._session_timeout_seconds is None


def test_invoke_once_uses_configured_idle_timeout(tmp_path, monkeypatch):
    """_invoke_once should pick up module-level idle_timeout via set_timeouts."""
    set_timeouts(1, None)

    # Make build_claude_cmd return a silent-sleep shell; _invoke_once should
    # raise IdleTimeoutError because idle trigger fires on the sleep.
    monkeypatch.setattr(
        agent, "build_claude_cmd",
        lambda *a, **k: ["sh", "-c", "sleep 5"],
    )
    from autocoder.sandbox import SandboxConfig
    sbx = SandboxConfig(allowed_tools=["Read"], docker=False)

    with pytest.raises(IdleTimeoutError):
        agent._invoke_once(
            prompt="",
            repo_path=str(tmp_path),
            model="sonnet",
            effort="max",
            max_budget_usd=1.0,
            sandbox=sbx,
            timeout=30,
        )
