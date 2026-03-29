from autocoder.types import RunConfig
from autocoder.verify import format_failure, run_verification


def _make_config(test_cmd=None, lint_cmd=None, integration_cmd=None):
    return RunConfig(
        repo_path="/tmp",
        labels=["P0"],
        test_cmd=test_cmd,
        lint_cmd=lint_cmd,
        integration_cmd=integration_cmd,
        model="sonnet",
        effort="max",
        triage_model="haiku",
        max_issues=10,
        max_analyze=0,
        max_turns=25,
        token_budget=500_000,
        daily_cap=5_000_000,
        docker=False,
        log_dir="/tmp/logs",
        dry_run=False,
        auto_prioritize=False,
        max_retries=3,
        protect_tests=False,
        test_patterns=[],
        auto_merge=False,
        plan_mode=False,
    )


def test_run_verification_passing():
    cfg = _make_config(lint_cmd="true", test_cmd="true")
    results = run_verification(cfg)
    assert len(results) == 2
    assert all(r.passed for r in results)


def test_run_verification_lint_fails():
    cfg = _make_config(lint_cmd="false", test_cmd="true")
    results = run_verification(cfg)
    assert len(results) == 1  # Stops at first failure
    assert not results[0].passed
    assert results[0].stage == "lint"


def test_run_verification_no_commands():
    cfg = _make_config()
    results = run_verification(cfg)
    assert len(results) == 0


def test_format_failure():
    from autocoder.types import VerifyResult
    result = VerifyResult(
        passed=False,
        stage="unit",
        exit_code=1,
        stdout="",
        stderr="FAIL: test_foo.py::test_bar - AssertionError\n" * 10,
        duration_ms=1000,
    )
    formatted = format_failure(result)
    assert "unit failed" in formatted
    assert "AssertionError" in formatted
