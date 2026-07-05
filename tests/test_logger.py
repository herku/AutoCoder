import json
import tempfile
from pathlib import Path

from autocoder.logger import RunLogger
from autocoder.types import AgentResult, Issue, Outcome, Priority, VerifyResult


def _make_issue():
    return Issue(
        number=42,
        title="Fix the widget",
        body="The widget is broken",
        labels=["P1", "bug"],
        priority=Priority.P1,
        url="https://github.com/test/repo/issues/42",
    )


def _make_agent_result():
    return AgentResult(
        session_id="sess-1",
        result_text="Fixed it",
        is_error=False,
        duration_ms=5000,
        tokens_in=1000,
        tokens_out=500,
        tokens_cached=200,
        cost_usd=0.02,
        num_turns=3,
        model="sonnet",
    )


def _make_verify_result(passed=True):
    return VerifyResult(
        passed=passed,
        stage="unit",
        exit_code=0 if passed else 1,
        stdout="ok" if passed else "FAIL",
        stderr="",
        duration_ms=2000,
    )


def test_log_attempt_writes_jsonl():
    with tempfile.TemporaryDirectory() as tmpdir:
        log = RunLogger(tmpdir)
        log.log_attempt(
            _make_issue(), 1, _make_agent_result(), [_make_verify_result()],
            Outcome.SUCCESS, pr_url="https://github.com/test/repo/pull/1",
        )
        log_files = list(Path(tmpdir).glob("run_*.jsonl"))
        assert len(log_files) == 1
        with open(log_files[0]) as f:
            record = json.loads(f.readline())
        assert record["issue_num"] == 42
        assert record["outcome"] == "success"
        assert record["pr_url"] == "https://github.com/test/repo/pull/1"


def test_dead_letter():
    with tempfile.TemporaryDirectory() as tmpdir:
        log = RunLogger(tmpdir)
        log.dead_letter(_make_issue(), "test failed 3 times")
        dl_path = Path(tmpdir) / "failed_issues.jsonl"
        assert dl_path.exists()
        with open(dl_path) as f:
            record = json.loads(f.readline())
        assert record["issue_num"] == 42
        assert "test failed" in record["error"]


def test_log_run_summary():
    with tempfile.TemporaryDirectory() as tmpdir:
        log = RunLogger(tmpdir)
        from autocoder.telemetry import Telemetry, Phase
        telem = Telemetry()
        telem.begin_issue(1, 1)
        telem.record_phase(Phase.IMPLEMENT, _make_agent_result())
        telem.end_issue(outcome="success")
        log.log_run_summary(telem)

        with open(log.log_path) as f:
            record = json.loads(f.readline())
        assert record["event"] == "run_summary"
        assert record["success_count"] == 1
        assert "phase_cost_breakdown" in record


def test_dead_letter_enriched_with_telemetry():
    with tempfile.TemporaryDirectory() as tmpdir:
        log = RunLogger(tmpdir)
        from autocoder.telemetry import Telemetry, Phase, FailureCategory
        telem = Telemetry()
        telem.begin_issue(42, 2)
        telem.record_phase(Phase.IMPLEMENT, _make_agent_result())
        telem.record_failure(FailureCategory.BUDGET_EXHAUSTED)
        issue_telem = telem.end_issue(outcome="skip")

        log.dead_letter(
            _make_issue(), "budget exhausted",
            telemetry=issue_telem, attempts=2, status_detail="BLOCKED: unclear",
        )
        with open(Path(tmpdir) / "failed_issues.jsonl") as f:
            record = json.loads(f.readline())
        assert record["failure_category"] == "budget_exhausted"
        assert record["last_phase"] == "implement"
        assert record["attempts"] == 2
        assert record["status_detail"] == "BLOCKED: unclear"
        assert record["cost_usd"] >= 0
        assert record["tokens_in"] > 0


def test_dead_letter_without_telemetry_still_minimal():
    with tempfile.TemporaryDirectory() as tmpdir:
        log = RunLogger(tmpdir)
        log.dead_letter(_make_issue(), "plain failure")
        with open(Path(tmpdir) / "failed_issues.jsonl") as f:
            record = json.loads(f.readline())
        assert "failure_category" not in record
        assert record["error"] == "plain failure"


def test_prior_failures_returns_matching_records():
    with tempfile.TemporaryDirectory() as tmpdir:
        log = RunLogger(tmpdir)
        log.dead_letter(_make_issue(), "first failure")
        log.dead_letter(_make_issue(), "second failure")
        other = Issue(99, "Other", "b", [], Priority.P2, "")
        log.dead_letter(other, "unrelated")

        priors = log.prior_failures(42)
        assert priors == ["first failure", "second failure"]
        assert log.prior_failures(1) == []


def test_prior_failures_includes_category_and_phase_prefix():
    with tempfile.TemporaryDirectory() as tmpdir:
        log = RunLogger(tmpdir)
        from autocoder.telemetry import Telemetry, Phase, FailureCategory
        telem = Telemetry()
        telem.begin_issue(42, 1)
        telem.record_phase(Phase.IMPLEMENT, _make_agent_result())
        telem.record_failure(FailureCategory.TEST_FAIL)
        issue_telem = telem.end_issue(outcome="skip")
        log.dead_letter(_make_issue(), "tests kept failing", telemetry=issue_telem)

        priors = log.prior_failures(42)
        assert len(priors) == 1
        assert "test_fail" in priors[0]
        assert "implement" in priors[0]
        assert "tests kept failing" in priors[0]


def test_prior_failures_respects_limit():
    with tempfile.TemporaryDirectory() as tmpdir:
        log = RunLogger(tmpdir)
        for i in range(5):
            log.dead_letter(_make_issue(), f"failure {i}")
        priors = log.prior_failures(42, limit=2)
        assert priors == ["failure 3", "failure 4"]
