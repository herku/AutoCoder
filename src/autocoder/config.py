from __future__ import annotations

import os
from pathlib import Path

from autocoder.types import RunConfig


def build_config(
    repo: str,
    labels: str,
    test_cmd: str | None,
    lint_cmd: str | None,
    integration_cmd: str | None,
    model: str,
    triage_model: str,
    max_issues: int,
    max_turns: int,
    token_budget: int,
    daily_cap: int,
    docker: bool,
    log_dir: str,
    dry_run: bool,
    max_retries: int,
    protect_tests: bool,
    test_patterns: str,
) -> RunConfig:
    repo_path = str(Path(repo).resolve())

    git_dir = Path(repo_path) / ".git"
    if not git_dir.exists():
        raise SystemExit(f"Error: {repo_path} is not a git repository")

    return RunConfig(
        repo_path=repo_path,
        labels=[l.strip() for l in labels.split(",") if l.strip()],
        test_cmd=test_cmd,
        lint_cmd=lint_cmd,
        integration_cmd=integration_cmd,
        model=model,
        triage_model=triage_model,
        max_issues=max_issues,
        max_turns=max_turns,
        token_budget=token_budget,
        daily_cap=daily_cap,
        docker=docker,
        log_dir=log_dir,
        dry_run=dry_run,
        max_retries=max_retries,
        protect_tests=protect_tests,
        test_patterns=[p.strip() for p in test_patterns.split(",") if p.strip()],
    )
