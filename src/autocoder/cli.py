from __future__ import annotations

import click

from autocoder.config import build_config
from autocoder.loop import run


@click.command()
@click.option("--repo", required=True, type=click.Path(exists=True), help="Path to target git repository")
@click.option("--labels", default=None, help="Comma-separated labels to filter by (omit to fetch all open issues)")
@click.option("--test-cmd", default=None, help="Test command (e.g. 'npm test')")
@click.option("--lint-cmd", default=None, help="Lint command (e.g. 'npm run lint')")
@click.option("--integration-cmd", default=None, help="Integration test command")
@click.option("--model", default="claude-opus-4-6", help="Claude model for coding tasks")
@click.option("--effort", default="max", type=click.Choice(["min", "low", "medium", "high", "max"]), help="Claude effort level")
@click.option("--triage-model", default="haiku", help="Claude model for issue triage/summarization")
@click.option("--max-issues", default=10, type=int, help="Maximum issues to process per run")
@click.option("--max-analyze", default=0, type=int, help="Maximum issues to fetch/analyze (0 = unlimited)")
@click.option("--max-turns", default=25, type=int, help="Max agent turns per issue (prompt guidance)")
@click.option("--token-budget", default=500_000, type=int, help="Token budget per issue")
@click.option("--daily-cap", default=5_000_000, type=int, help="Daily token cap across all issues")
@click.option("--docker", is_flag=True, default=False, help="Run agent inside Docker sandbox")
@click.option("--log-dir", default="./logs", help="Directory for JSONL log files")
@click.option("--dry-run", is_flag=True, default=False, help="Fetch issues and show plan without executing")
@click.option("--auto-prioritize/--no-auto-prioritize", default=True, help="Use AI to analyze and prioritize issues by automability")
@click.option("--max-retries", default=3, type=int, help="Max retry attempts per issue")
@click.option("--protect-tests", is_flag=True, default=False, help="Prevent agent from modifying test files")
@click.option("--auto-merge", is_flag=True, default=False, help="Auto-review, fix, and squash-merge PRs after creation")
@click.option("--issue", "issues", multiple=True, type=int, help="Specific issue number(s) to process (skips fetch and auto-prioritize)")
@click.option("--plan-mode", is_flag=True, default=False, help="Agent plans before implementing each issue")
@click.option(
    "--test-patterns",
    default="**/test_*,**/*_test.*,**/tests/**,**/*.test.*,**/*.spec.*",
    help="Comma-separated glob patterns for test files",
)
def main(**kwargs: object) -> None:
    """AutoCoder: Autonomous AI coding agent loop.

    Fetches GitHub issues, resolves them with Claude Code, runs tests, and ships draft PRs.
    """
    cfg = build_config(**kwargs)
    run(cfg)


if __name__ == "__main__":
    main()
