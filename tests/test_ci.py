import subprocess
from unittest.mock import patch, MagicMock, call

from autocoder.pr import wait_for_ci, wait_for_new_checks
from autocoder.prompts import load
from autocoder.review import build_ci_fix_prompt, CI_OUTPUT_MAX
from autocoder.types import CIResult


def test_wait_for_ci_passes():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "All checks passed"
    with patch("autocoder.pr.subprocess.run", return_value=mock_result) as mock_run:
        result = wait_for_ci("/repo", "https://github.com/o/r/pull/42", 300)
    assert result.passed is True
    assert result.timed_out is False
    assert result.output == "All checks passed"
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert "42" in args
    assert "--watch" in args
    assert "--timeout" in args
    assert "300" in args


def test_wait_for_ci_fails():
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = "build\tfail\n"
    mock_result.stderr = "Some checks were not successful"
    with patch("autocoder.pr.subprocess.run", return_value=mock_result):
        result = wait_for_ci("/repo", "https://github.com/o/r/pull/42", 300)
    assert result.passed is False
    assert result.timed_out is False
    assert "fail" in result.output


def test_wait_for_ci_timeout():
    with patch("autocoder.pr.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=330)):
        result = wait_for_ci("/repo", "https://github.com/o/r/pull/42", 300)
    assert result.passed is False
    assert result.timed_out is True
    assert result.output == ""


def test_build_ci_fix_prompt():
    prompt = build_ci_fix_prompt("Error: test_login failed\nAssertionError")
    assert "Error: test_login failed" in prompt
    assert "Fix ONLY" in prompt
    assert "CI checks failed" in prompt


def test_build_ci_fix_prompt_truncates():
    long_output = "x" * (CI_OUTPUT_MAX + 1000)
    prompt = build_ci_fix_prompt(long_output)
    assert len(prompt) < CI_OUTPUT_MAX + 500  # template overhead


def test_ci_fix_template_loads():
    template = load("ci_fix")
    assert "{ci_output}" in template
    assert "CI" in template


def test_ci_result_dataclass():
    r = CIResult(passed=True, output="ok", timed_out=False)
    assert r.passed is True
    assert r.output == "ok"
    assert r.timed_out is False


def test_wait_for_new_checks_found_immediately():
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "2\n"
    with patch("autocoder.pr.subprocess.run", return_value=mock_result):
        assert wait_for_new_checks("/repo", "abc123", timeout=10) is True


def test_wait_for_new_checks_found_after_retry():
    no_checks = MagicMock(returncode=0, stdout="0\n")
    has_checks = MagicMock(returncode=0, stdout="1\n")
    with patch("autocoder.pr.subprocess.run", side_effect=[no_checks, has_checks]), \
         patch("autocoder.pr.time.sleep"):
        assert wait_for_new_checks("/repo", "abc123", timeout=60) is True


def test_wait_for_new_checks_timeout():
    no_checks = MagicMock(returncode=0, stdout="0\n")
    # monotonic calls: deadline=0, while-check=50, sleep-arg=55, while-check=121 (exits)
    with patch("autocoder.pr.subprocess.run", return_value=no_checks), \
         patch("autocoder.pr.time.sleep"), \
         patch("autocoder.pr.time.monotonic", side_effect=[0, 50, 55, 121]):
        assert wait_for_new_checks("/repo", "abc123", timeout=120) is False


def test_wait_for_new_checks_api_error():
    bad_result = MagicMock(returncode=1, stdout="")
    good_result = MagicMock(returncode=0, stdout="1\n")
    with patch("autocoder.pr.subprocess.run", side_effect=[bad_result, good_result]), \
         patch("autocoder.pr.time.sleep"):
        assert wait_for_new_checks("/repo", "abc123", timeout=60) is True
