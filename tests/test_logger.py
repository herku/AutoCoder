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
