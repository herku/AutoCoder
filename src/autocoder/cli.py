from __future__ import annotations

import click

from autocoder.config import build_config
from autocoder.loop import run


@click.command()
@click.option("--repo", required=True, type=click.Path(exists=True), help="Path to target git repository")
@click.option("--labels", default=None, help="Comma-separated labels to filter by (omit to fetch all open issues)")
@click.option("--build-cmd", default=None, help="Build command (e.g. 'npm run build'). Auto-detected if not specified.")
@click.option("--build-retries", default=1, type=int, help="Max retries for build failures (default 1)")
@click.option("--test-cmd", default=None, help="Test command (e.g. 'npm test')")
@click.option("--lint-cmd", default=None, help="Lint command (e.g. 'npm run lint')")
@click.option("--integration-cmd", default=None, help="Integration test command")
@click.option("--model", default="claude-sonnet-5", help="Claude model for implementation, review, and fix tasks")
@click.option("--plan-model", default="claude-opus-4-8", help="Claude model for planning phase (requires --plan-mode)")
@click.option("--review-model", default="claude-opus-4-8", help="Claude model for code review (requires --auto-merge)")
@click.option("--effort", default="max", type=click.Choice(["min", "low", "medium", "high", "max"]), help="Claude effort level")
@click.option("--triage-model", default="haiku", help="Claude model for issue triage/summarization")
@click.option("--max-issues", default=10, type=int, help="Maximum issues to process per run")
@click.option("--max-analyze", default=0, type=int, help="Maximum issues to fetch/analyze (0 = unlimited)")
@click.option("--max-turns", default=25, type=int, help="Max agent turns per issue (prompt guidance)")
@click.option("--token-budget", default=500_000, type=int, help="Token budget per issue")
@click.option("--daily-cap", default=5_000_000, type=int, help="Daily token cap across all issues")
@click.option("--docker", is_flag=True, default=False, help="Run agent inside Docker sandbox")
@click.option("--update-docker", is_flag=True, default=False, help="Force-rebuild Docker image with latest Claude Code")
@click.option("--docker-max-age-days", default=7, type=int, help="Auto-rebuild Docker image if older than N days (default: 7)")
@click.option("--log-dir", default="./logs", help="Directory for JSONL log files")
@click.option("--dry-run", is_flag=True, default=False, help="Fetch issues and show plan without executing")
@click.option("--auto-prioritize/--no-auto-prioritize", default=True, help="Use AI to analyze and prioritize issues by automability")
@click.option("--force-prioritize", is_flag=True, default=False, help="Bypass prioritization cache and re-run AI analysis")
@click.option("--max-retries", default=3, type=int, help="Max retry attempts per issue")
@click.option("--protect-tests", is_flag=True, default=False, help="Prevent agent from modifying test files")
@click.option("--auto-merge", is_flag=True, default=False, help="Auto-review, fix, and squash-merge PRs after creation")
@click.option("--ci-timeout", default=1800, type=int, help="CI check timeout in seconds (default 1800)")
@click.option("--issue", "issues", multiple=True, type=int, help="Specific issue number(s) to process (skips fetch and auto-prioritize)")
@click.option("--plan-mode", is_flag=True, default=False, help="Agent plans before implementing each issue")
@click.option("--update-claude-md/--no-update-claude-md", default=True, help="Update repo CLAUDE.md before committing")
@click.option(
    "--test-patterns",
    default="**/test_*,**/*_test.*,**/tests/**,**/*.test.*,**/*.spec.*",
    help="Comma-separated glob patterns for test files",
)
@click.option(
    "--wait-on-rate-limit",
    default=None,
    help="On rate-limit errors, wait this duration (e.g. '30s', '5m', '1h') and retry up to 3 times. Default: abort immediately.",
)
@click.option(
    "--stalemate-threshold",
    default=2,
    type=int,
    help="Abort review/CI-fix loops after N consecutive no-change iterations (default 2).",
)
@click.option(
    "--review-mode",
    default="single",
    type=click.Choice(["single", "multi"]),
    help="Code review mode: 'single' (one reviewer) or 'multi' (5 parallel specialized reviewers that also fix in-session).",
)
@click.option(
    "--review-budget-usd",
    default=2.00,
    type=float,
    help="Budget cap for the multi-agent review orchestrator (default $2.00).",
)
@click.option(
    "--external-reviewer",
    default=None,
    help="Second-opinion reviewer. Pass a preset name ('codex', 'gemini', 'claude') or a full shell command; prompt is piped on stdin. Examples: 'codex', 'codex exec -m gpt-5', 'claude -p --model claude-opus-4-8 --output-format text'.",
)
@click.option(
    "--implement-brief/--no-implement-brief",
    default=True,
    help="Before each implement phase, spawn 3 parallel advisors (architecture/tests/risks) to generate a design brief prepended to the implementer's prompt. Default: on.",
)
@click.option(
    "--brief-budget-usd",
    default=1.00,
    type=float,
    help="Budget cap for the pre-implement brief orchestrator (default $1.00).",
)
@click.option(
    "--pre-verify-critique/--no-pre-verify-critique",
    default=True,
    help="After each implement phase and before verification, run multi-agent critique to catch obvious defects. Reuses the review_multi orchestrator. Default: on.",
)
@click.option(
    "--pre-verify-budget-usd",
    default=1.50,
    type=float,
    help="Budget cap for the pre-verify critique orchestrator (default $1.50).",
)
@click.option(
    "--idle-timeout",
    default=None,
    help="Kill Claude subprocess if no stdout for this long (e.g. '5m', '90s'). Default: disabled.",
)
@click.option(
    "--session-timeout",
    default=None,
    help="Hard cap on Claude subprocess duration (e.g. '30m', '1h'). Fires before per-phase wall timeout. Default: disabled.",
)
@click.option(
    "--serve/--no-serve",
    default=False,
    help="Start a localhost dashboard that streams live run events via SSE.",
)
@click.option(
    "--port",
    default=8765,
    type=int,
    help="Dashboard port when --serve is set (default 8765).",
)
@click.option(
    "--task-slice/--no-task-slice",
    default=None,
    help="Slice the implement phase into tasks executed in fresh Claude sessions. "
    "Default: auto (enable when the issue has ≥3 acceptance criteria or body > 1500 chars).",
)
@click.option(
    "--task-retries",
    default=1,
    type=int,
    help="Per-task retry count within the task-slice loop (default 1).",
)
@click.option(
    "--max-tasks",
    default=15,
    type=int,
    help="Cap on task count; plans above this fall back to monolithic implement (default 15).",
)
@click.option(
    "--parallel",
    default=1,
    type=int,
    help="Process N issues concurrently, each in its own git worktree (default 1).",
)
@click.option(
    "--worktree-root",
    default=None,
    help="Directory for per-issue git worktrees (default: <repo>/.autocoder/worktrees).",
)
@click.option(
    "--escalate-on-block/--no-escalate-on-block",
    default=True,
    help="When the implementer reports STATUS: BLOCKED or NEEDS_CONTEXT, retry once with --escalation-model. Default: on.",
)
@click.option(
    "--escalation-model",
    default="claude-opus-4-8",
    help="Stronger Claude model used only for BLOCKED/NEEDS_CONTEXT in-attempt retries (default claude-opus-4-8).",
)
@click.option(
    "--ci-arch-review/--no-ci-arch-review",
    default=True,
    help="When CI-fix attempts approach the stalemate threshold, run an architectural critique to question the pattern instead of patching again. Default: on.",
)
def main(**kwargs: object) -> None:
    """AutoCoder: Autonomous AI coding agent loop.

    Fetches GitHub issues, resolves them with Claude Code, runs tests, and ships draft PRs.
    """
    cfg = build_config(**kwargs)
    run(cfg)


if __name__ == "__main__":
    main()
