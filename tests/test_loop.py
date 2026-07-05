from unittest.mock import MagicMock, patch

import pytest

from autocoder.loop import (
    StalemateTracker, _handle_implementer_status, _run_ci_arch_review,
)
from autocoder.telemetry import Phase, Telemetry
from autocoder.types import (
    AgentResult, ImplementerBlockedError, ImplementerStatus, RunConfig,
)
from autocoder.sandbox import SandboxConfig


def test_stalemate_not_triggered_on_change():
    t = StalemateTracker(threshold=2)
    assert not t.note("aaa", "bbb")
    assert not t.note("bbb", "ccc")
    assert t.streak == 0


def test_stalemate_triggered_after_threshold_unchanged():
    t = StalemateTracker(threshold=2)
    assert not t.note("aaa", "aaa")  # streak=1, threshold=2 → not yet
    assert t.note("aaa", "aaa")  # streak=2 → stalemate


def test_stalemate_resets_on_change():
    t = StalemateTracker(threshold=2)
    assert not t.note("aaa", "aaa")  # streak=1
    assert not t.note("aaa", "bbb")  # reset to 0
    assert not t.note("bbb", "bbb")  # streak=1
    assert t.note("bbb", "bbb")  # streak=2


def test_stalemate_threshold_one_triggers_immediately():
    t = StalemateTracker(threshold=1)
    assert t.note("aaa", "aaa")


def test_stalemate_default_threshold_is_two():
    t = StalemateTracker()
    assert not t.note("a", "a")
    assert t.note("a", "a")


def test_new_phases_registered():
    # Plan 2 adds two new phase telemetry slots
    assert Phase.IMPLEMENT_BRIEF.value == "implement_brief"
    assert Phase.PRE_VERIFY_CRITIQUE.value == "pre_verify_critique"
    # Two-stage review + arch escalation
    assert Phase.REVIEW_SPEC_COMPLIANCE.value == "review_spec_compliance"
    assert Phase.REVIEW_QUALITY.value == "review_quality"
    assert Phase.CI_FIX_ARCH.value == "ci_fix_arch"


# ---------- implementer status branching ----------


def _result(text: str = "ok", status: ImplementerStatus | None = None,
            detail: str | None = None, model: str = "sonnet") -> AgentResult:
    return AgentResult(
        session_id="s", result_text=text, is_error=False, duration_ms=1,
        tokens_in=0, tokens_out=0, tokens_cached=0, cost_usd=0.0, num_turns=1,
        model=model, status=status, status_detail=detail,
    )


def _cfg(**overrides) -> RunConfig:
    base = dict(
        repo_path="/tmp/x", labels=[], test_cmd=None, lint_cmd=None,
        integration_cmd=None, model="sonnet", plan_model="opus",
        review_model="opus", effort="max", triage_model="haiku",
        max_issues=1, max_analyze=0, max_turns=10, token_budget=10_000,
        daily_cap=100_000, docker=False, log_dir="/tmp/logs", dry_run=False,
        auto_prioritize=False, max_retries=1, protect_tests=False,
        test_patterns=[], auto_merge=False, plan_mode=False,
    )
    base.update(overrides)
    return RunConfig(**base)


def _telem_with_issue(num: int = 1) -> Telemetry:
    t = Telemetry()
    t.begin_issue(num, attempt=1, title="t")
    return t


def _budget():
    bt = MagicMock()
    bt.remaining_for_issue_usd.return_value = 5.0
    return bt


def _timings():
    from autocoder.loop import StepTimings
    return StepTimings()


def _sbx() -> SandboxConfig:
    return SandboxConfig(allowed_tools=["Read"], docker=False)


def test_status_done_passes_through():
    r = _result(status=ImplementerStatus.DONE)
    out = _handle_implementer_status(
        r, "prompt", _cfg(), _sbx(), _budget(), _telem_with_issue(),
        "#1", "att1", _timings(),
    )
    assert out is r  # same object, no escalation


def test_status_done_with_concerns_passes_through_and_logs():
    r = _result(status=ImplementerStatus.DONE_WITH_CONCERNS, detail="flaky")
    telem = _telem_with_issue()
    out = _handle_implementer_status(
        r, "prompt", _cfg(), _sbx(), _budget(), telem,
        "#1", "att1", _timings(),
    )
    assert out is r


def test_status_blocked_escalates_to_stronger_model():
    blocked = _result(status=ImplementerStatus.BLOCKED, detail="ambiguous spec")
    escalated = _result(text="fixed", status=ImplementerStatus.DONE,
                        model="claude-opus-4-7")
    with patch("autocoder.loop.invoke_agent", return_value=escalated) as mock:
        out = _handle_implementer_status(
            blocked, "original prompt", _cfg(escalation_model="claude-opus-4-7"),
            _sbx(), _budget(), _telem_with_issue(), "#1", "att1", _timings(),
        )
    assert out is escalated
    # Was called with the escalation model, not the base model
    assert mock.call_args.args[2] == "claude-opus-4-7"
    # Prompt was augmented with the prior status detail
    assert "ambiguous spec" in mock.call_args.args[0]


def test_status_blocked_then_blocked_again_raises():
    blocked = _result(status=ImplementerStatus.BLOCKED, detail="still stuck")
    re_blocked = _result(status=ImplementerStatus.BLOCKED, detail="opus also stuck")
    with patch("autocoder.loop.invoke_agent", return_value=re_blocked):
        with pytest.raises(ImplementerBlockedError) as ei:
            _handle_implementer_status(
                blocked, "p", _cfg(), _sbx(), _budget(), _telem_with_issue(),
                "#1", "att1", _timings(),
            )
    assert "opus also stuck" in str(ei.value)


def test_status_needs_context_treated_like_blocked():
    needs = _result(status=ImplementerStatus.NEEDS_CONTEXT, detail="missing API")
    fixed = _result(status=ImplementerStatus.DONE)
    with patch("autocoder.loop.invoke_agent", return_value=fixed):
        out = _handle_implementer_status(
            needs, "p", _cfg(), _sbx(), _budget(), _telem_with_issue(),
            "#1", "att1", _timings(),
        )
    assert out is fixed


def test_blocked_with_escalation_disabled_raises():
    blocked = _result(status=ImplementerStatus.BLOCKED, detail="x")
    with pytest.raises(ImplementerBlockedError):
        _handle_implementer_status(
            blocked, "p", _cfg(escalate_on_block=False), _sbx(), _budget(),
            _telem_with_issue(), "#1", "att1", _timings(),
        )


# ---------- implement retry-context accumulation ----------


def _issue(num: int = 7, body: str = "Fix the thing."):
    from autocoder.types import Issue, Priority
    return Issue(number=num, title="Fix", body=body, labels=[],
                 priority=Priority.P2, url="https://example/7")


def _err_result(text: str) -> AgentResult:
    r = _result(text=text)
    r.is_error = True
    return r


def test_format_impl_attempt_caps_length():
    from autocoder.loop import _format_impl_attempt, _IMPL_ATTEMPT_MAX
    formatted = _format_impl_attempt(2, "x" * 10_000)
    assert "## Attempt 2 failed" in formatted
    assert len(formatted) < _IMPL_ATTEMPT_MAX + 100


def test_process_issue_accumulates_failure_context_across_attempts():
    from autocoder.loop import process_issue, StepTimings
    prompts: list[str] = []

    def fake_invoke(prompt, *args, **kwargs):
        prompts.append(prompt)
        return _err_result(f"FAILURE_MARKER_{len(prompts)}")

    cfg = _cfg(max_retries=3, implement_brief=False, pre_verify_critique=False)
    with patch("autocoder.loop.invoke_agent", side_effect=fake_invoke), \
         patch("autocoder.loop.label_failed"), \
         patch("autocoder.loop.comment_failure"):
        process_issue(_issue(), cfg, MagicMock(), _budget(), MagicMock(),
                      StepTimings(), Telemetry())

    assert len(prompts) == 3
    assert "FAILURE_MARKER_1" not in prompts[0]
    # Attempt 2 sees attempt 1's failure; attempt 3 sees BOTH prior failures.
    assert "FAILURE_MARKER_1" in prompts[1]
    assert "FAILURE_MARKER_1" in prompts[2]
    assert "FAILURE_MARKER_2" in prompts[2]


def test_process_issue_latest_failure_not_lost_after_earlier_verify_failure():
    # Regression for the stale-context bug: attempt 1 fails verification
    # (which sets error_context), attempt 2 fails with an agent error — the
    # old `if not error_context` guard dropped attempt 2's failure entirely.
    from autocoder.loop import process_issue, StepTimings
    from autocoder.types import VerifyResult
    prompts: list[str] = []
    calls = {"n": 0}

    def fake_invoke(prompt, *args, **kwargs):
        prompts.append(prompt)
        calls["n"] += 1
        if calls["n"] == 1:
            return _result(text="implemented")
        return _err_result("AGENT_EXPLODED_ATTEMPT_2")

    failing = [VerifyResult(passed=False, stage="unit", exit_code=1,
                            stdout="assert failed in test_foo_bar", stderr="",
                            duration_ms=1)]
    cfg = _cfg(max_retries=3, implement_brief=False, pre_verify_critique=False)
    with patch("autocoder.loop.invoke_agent", side_effect=fake_invoke), \
         patch("autocoder.loop.run_verification", return_value=failing), \
         patch("autocoder.loop.label_failed"), \
         patch("autocoder.loop.comment_failure"):
        process_issue(_issue(), cfg, MagicMock(), _budget(), MagicMock(),
                      StepTimings(), Telemetry())

    assert len(prompts) == 3
    assert "test_foo_bar" in prompts[1]
    # The fresh attempt-2 failure MUST reach attempt 3's context.
    assert "AGENT_EXPLODED_ATTEMPT_2" in prompts[2]
    # And the older failure is still present (accumulated, not replaced).
    assert "test_foo_bar" in prompts[2]


# ---------- ci_fix_arch escalation ----------


def test_ci_arch_review_returns_text_and_records_phase():
    arch = _result(text="## Pattern\n- attempt 1: cast to any → still fails\n\n## Recommendation\nREFACTOR")
    telem = _telem_with_issue()
    bt = _budget()
    with patch("autocoder.loop.invoke_agent", return_value=arch):
        rec = _run_ci_arch_review(
            _cfg(), bt, telem, "ci out", "## CI Fix Attempt 1\n...",
        )
    assert "REFACTOR" in rec
    # Phase recorded
    phases = [p.phase for p in telem._get_current().phases]
    assert Phase.CI_FIX_ARCH in phases


def test_ci_arch_review_skipped_when_budget_exhausted():
    bt = MagicMock()
    bt.remaining_for_issue_usd.return_value = 0.01  # under threshold
    rec = _run_ci_arch_review(_cfg(), bt, _telem_with_issue(), "ci", "prior")
    assert rec == ""


def test_ci_arch_review_swallows_agent_error_returns_empty():
    err = _result()
    err.is_error = True
    with patch("autocoder.loop.invoke_agent", return_value=err):
        rec = _run_ci_arch_review(_cfg(), _budget(), _telem_with_issue(), "c", "p")
    assert rec == ""
