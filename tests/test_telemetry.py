from __future__ import annotations

import pytest

from autocoder.telemetry import (
    FailureCategory,
    IssueTelemetry,
    Phase,
    PhaseMetrics,
    RunSummary,
    Telemetry,
    VerifyStageMetrics,
)
from autocoder.types import AgentResult, ReviewFinding, ReviewResult, PlanCheckItem, TestPlanResult, VerifyResult


def _make_agent_result(**overrides) -> AgentResult:
    defaults = dict(
        session_id="s1", result_text="ok", is_error=False,
        duration_ms=1000, tokens_in=100, tokens_out=50,
        tokens_cached=80, cost_usd=0.01, num_turns=5, model="sonnet",
    )
    defaults.update(overrides)
    return AgentResult(**defaults)


class TestPhaseMetrics:
    def test_cache_hit_rate(self):
        pm = PhaseMetrics(
            phase=Phase.IMPLEMENT, model="sonnet",
            tokens_in=1000, tokens_out=500, tokens_cached=800,
            cost_usd=0.05, duration_ms=5000, num_turns=10,
        )
        assert pm.cache_hit_rate == pytest.approx(0.8)

    def test_cache_hit_rate_zero_input(self):
        pm = PhaseMetrics(
            phase=Phase.PLAN, model="opus",
            tokens_in=0, tokens_out=0, tokens_cached=0,
            cost_usd=0, duration_ms=0, num_turns=0,
        )
        assert pm.cache_hit_rate == 0.0


class TestIssueTelemetry:
    def test_aggregation_properties(self):
        it = IssueTelemetry(issue_number=1, attempt=1)
        it.phases = [
            PhaseMetrics(Phase.PLAN, "opus", 100, 50, 80, 0.10, 1000, 3),
            PhaseMetrics(Phase.IMPLEMENT, "sonnet", 200, 100, 150, 0.20, 5000, 10),
        ]
        assert it.total_tokens_in == 300
        assert it.total_tokens_out == 150
        assert it.total_tokens_cached == 230
        assert it.total_cost_usd == pytest.approx(0.30)
        assert it.cache_hit_rate == pytest.approx(230 / 300)

    def test_empty_phases(self):
        it = IssueTelemetry(issue_number=1, attempt=1)
        assert it.total_tokens_in == 0
        assert it.cache_hit_rate == 0.0
        assert it.total_cost_usd == 0.0


class TestTelemetry:
    def test_begin_record_end_lifecycle(self):
        t = Telemetry()
        t.begin_issue(42, 1)
        t.record_phase(Phase.IMPLEMENT, _make_agent_result())
        result = t.end_issue(outcome="success")

        assert result is not None
        assert result.issue_number == 42
        assert result.attempt == 1
        assert len(result.phases) == 1
        assert result.phases[0].phase == Phase.IMPLEMENT
        assert result.outcome == "success"
        assert len(t.completed) == 1

    def test_record_verify(self):
        t = Telemetry()
        t.begin_issue(1, 1)
        t.record_verify([
            VerifyResult(passed=True, stage="lint", exit_code=0, stdout="", stderr="", duration_ms=500),
            VerifyResult(passed=False, stage="unit", exit_code=1, stdout="", stderr="fail", duration_ms=3000),
        ])
        result = t.end_issue()
        assert len(result.verify_stages) == 2
        assert result.verify_stages[0].passed is True
        assert result.verify_stages[1].passed is False
        assert result.verify_stages[1].duration_ms == 3000

    def test_record_review(self):
        t = Telemetry()
        t.begin_issue(1, 1)
        review = ReviewResult(
            findings=[ReviewFinding("critical", "app.py", "SQL injection")],
            raw_response="", has_actionable_issues=True,
        )
        t.record_review(review)
        result = t.end_issue()
        assert len(result.review_findings) == 1
        assert result.review_findings[0]["severity"] == "critical"

    def test_record_testplan(self):
        t = Telemetry()
        t.begin_issue(1, 1)
        tp = TestPlanResult(
            items=[PlanCheckItem("Add widget", "pass", "widget.py exists")],
            raw_response="", all_passed=True,
        )
        t.record_testplan(tp)
        result = t.end_issue()
        assert len(result.testplan_items) == 1
        assert result.testplan_items[0]["status"] == "pass"

    def test_record_failure(self):
        t = Telemetry()
        t.begin_issue(1, 1)
        t.record_failure(FailureCategory.LINT_FAIL)
        result = t.end_issue()
        assert result.failure_category == FailureCategory.LINT_FAIL

    def test_no_current_issue_is_safe(self):
        """Calling record_* without begin_issue should not crash."""
        t = Telemetry()
        t.record_phase(Phase.PLAN, _make_agent_result())
        t.record_verify([])
        t.record_failure(FailureCategory.TIMEOUT)
        assert t.end_issue() is None

    def test_auto_end_on_begin(self):
        """Starting a new issue auto-ends the previous if not ended."""
        t = Telemetry()
        t.begin_issue(1, 1)
        t.record_phase(Phase.IMPLEMENT, _make_agent_result())
        t.begin_issue(2, 1)  # Should auto-end issue 1
        t.end_issue()
        assert len(t.completed) == 2
        assert t.completed[0].issue_number == 1

    def test_to_jsonl_dict(self):
        t = Telemetry()
        t.begin_issue(42, 1)
        t.record_phase(Phase.IMPLEMENT, _make_agent_result(tokens_in=1000, tokens_cached=800))
        t.record_failure(FailureCategory.TEST_FAIL)
        result = t.end_issue()
        d = Telemetry.to_jsonl_dict(result)

        assert "phase_metrics" in d
        assert len(d["phase_metrics"]) == 1
        assert d["phase_metrics"][0]["phase"] == "implement"
        assert d["failure_category"] == "test_fail"
        assert d["cache_hit_rate"] == pytest.approx(0.8)
        assert "total_phase_cost_usd" in d

    def test_run_summary(self):
        t = Telemetry()

        # Issue 1: success
        t.begin_issue(1, 1)
        t.record_phase(Phase.PLAN, _make_agent_result(cost_usd=0.50, model="opus"))
        t.record_phase(Phase.IMPLEMENT, _make_agent_result(cost_usd=1.00, model="sonnet"))
        t.end_issue(outcome="success")

        # Issue 2: retry then skip
        t.begin_issue(2, 1)
        t.record_phase(Phase.IMPLEMENT, _make_agent_result(cost_usd=0.30, model="sonnet"))
        t.record_failure(FailureCategory.TEST_FAIL)
        t.end_issue(outcome="retry")

        t.begin_issue(2, 2)
        t.record_phase(Phase.IMPLEMENT, _make_agent_result(cost_usd=0.40, model="sonnet"))
        t.record_failure(FailureCategory.TEST_FAIL)
        t.end_issue(outcome="skip")

        s = t.run_summary()
        assert s.issues_processed == 2
        assert s.success_count == 1
        assert s.retry_count == 1
        assert s.skip_count == 1
        assert s.total_cost_usd == pytest.approx(2.20)
        assert "plan" in s.phase_cost_breakdown
        assert "implement" in s.phase_cost_breakdown
        assert s.per_model_cost["opus"] == pytest.approx(0.50)
        assert s.per_model_cost["sonnet"] == pytest.approx(1.70)
        assert s.top_failure_reasons[0] == ("test_fail", 2)
