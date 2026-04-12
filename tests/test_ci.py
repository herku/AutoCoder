import subprocess
from unittest.mock import patch, MagicMock

from autocoder.pr import wait_for_ci
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
