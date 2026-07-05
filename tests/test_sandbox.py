from autocoder.sandbox import build_sandbox
from autocoder.types import RunConfig


def _cfg(**overrides) -> RunConfig:
    base = dict(
        repo_path="/tmp/x", labels=[], test_cmd="uv run pytest", lint_cmd=None,
        integration_cmd=None, model="sonnet", plan_model="opus",
        review_model="opus", effort="max", triage_model="haiku",
        max_issues=1, max_analyze=0, max_turns=10, token_budget=10_000,
        daily_cap=100_000, docker=False, log_dir="/tmp/logs", dry_run=False,
        auto_prioritize=False, max_retries=1, protect_tests=False,
        test_patterns=[], auto_merge=False, plan_mode=False,
    )
    base.update(overrides)
    return RunConfig(**base)


def test_build_sandbox_adds_exact_and_prefix_wildcard():
    sandbox = build_sandbox(_cfg())
    assert "Bash(uv run pytest)" in sandbox.allowed_tools
    assert "Bash(uv run pytest:*)" in sandbox.allowed_tools


def test_build_sandbox_wildcard_is_full_command_not_first_token():
    sandbox = build_sandbox(_cfg(test_cmd="npm run test"))
    assert "Bash(npm run test:*)" in sandbox.allowed_tools
    # The wildcard must never be derived from just the first token.
    assert "Bash(npm run:*)" not in sandbox.allowed_tools


def test_build_sandbox_skips_unconfigured_commands():
    sandbox = build_sandbox(_cfg(test_cmd=None))
    assert not any("pytest" in t for t in sandbox.allowed_tools)


def test_build_sandbox_all_verification_commands_get_wildcards():
    sandbox = build_sandbox(_cfg(
        test_cmd="pytest", lint_cmd="ruff check .", integration_cmd="pytest -m integration",
    ))
    for cmd in ("pytest", "ruff check .", "pytest -m integration"):
        assert f"Bash({cmd})" in sandbox.allowed_tools
        assert f"Bash({cmd}:*)" in sandbox.allowed_tools
