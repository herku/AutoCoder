from __future__ import annotations

import click

from autocoder.config import build_config
from autocoder.loop import run


@click.command()
@click.option("--repo", required=True, type=click.Path(exists=True), help="Path to target git repository")
@click.option("--labels", default="P0,P1,P2", help="Comma-separated priority labels to process")
@click.option("--test-cmd", default=None, help="Test command (e.g. 'npm test')")
@click.option("--lint-cmd", default=None, help="Lint command (e.g. 'npm run lint')")
@click.option("--integration-cmd", default=None, help="Integration test command")
@click.option("--model", default="sonnet", help="Claude model for coding tasks")
@click.option("--triage-model", default="haiku", help="Claude model for issue triage/summarization")
@click.option("--max-issues", default=10, type=int, help="Maximum issues to process per run")
@click.option("--max-turns", default=25, type=int, help="Max agent turns per issue (prompt guidance)")
@click.option("--token-budget", default=500_000, type=int, help="Token budget per issue")
@click.option("--daily-cap", default=5_000_000, type=int, help="Daily token cap across all issues")
@click.option("--docker", is_flag=True, default=False, help="Run agent inside Docker sandbox")
@click.option("--log-dir", default="./logs", help="Directory for JSONL log files")
@click.option("--dry-run", is_flag=True, default=False, help="Fetch issues and show plan without executing")
@click.option("--max-retries", default=3, type=int, help="Max retry attempts per issue")
@click.option("--protect-tests", is_flag=True, default=False, help="Prevent agent from modifying test files")
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
