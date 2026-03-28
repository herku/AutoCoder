from __future__ import annotations

import os
import subprocess
from pathlib import Path

from autocoder.types import RunConfig


def build_config(
    repo: str,
    labels: str | None,
    test_cmd: str | None,
    lint_cmd: str | None,
    integration_cmd: str | None,
    model: str,
    triage_model: str,
    max_issues: int,
    max_analyze: int,
    max_turns: int,
    token_budget: int,
    daily_cap: int,
    docker: bool,
    log_dir: str,
    dry_run: bool,
    auto_prioritize: bool,
    max_retries: int,
    protect_tests: bool,
    test_patterns: str,
) -> RunConfig:
    repo_path = str(Path(repo).resolve())

    git_dir = Path(repo_path) / ".git"
    if not git_dir.exists():
        raise SystemExit(f"Error: {repo_path} is not a git repository")

    if docker:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise SystemExit(
                "Error: --docker is enabled but Docker is not running.\n"
                "Start Docker Desktop or remove --docker to run without sandboxing."
            )
        result = subprocess.run(
            ["docker", "image", "inspect", "autocoder-sandbox"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            dockerfile = Path(__file__).resolve().parent.parent.parent / "Dockerfile"
            if not dockerfile.exists():
                raise SystemExit(
                    "Error: 'autocoder-sandbox' image not found and no Dockerfile available.\n"
                    "Remove --docker to run without sandboxing."
                )
            print("Docker image 'autocoder-sandbox' not found. Building...")
            build = subprocess.run(
                ["docker", "build", "-t", "autocoder-sandbox", str(dockerfile.parent)],
                check=False,
            )
            if build.returncode != 0:
                raise SystemExit(
                    "Error: Failed to build 'autocoder-sandbox' Docker image.\n"
                    "Remove --docker to run without sandboxing."
                )
        # Check host auth files exist
        home = Path.home()
        if not (home / ".claude.json").exists():
            raise SystemExit(
                "Error: ~/.claude.json not found. Claude is not logged in.\n"
                "Run 'claude login' first, then retry with --docker."
            )

    return RunConfig(
        repo_path=repo_path,
        labels=[l.strip() for l in labels.split(",") if l.strip()] if labels else [],
        test_cmd=test_cmd,
        lint_cmd=lint_cmd,
        integration_cmd=integration_cmd,
        model=model,
        triage_model=triage_model,
        max_issues=max_issues,
        max_analyze=max_analyze,
        max_turns=max_turns,
        token_budget=token_budget,
        daily_cap=daily_cap,
        docker=docker,
        log_dir=log_dir,
        dry_run=dry_run,
        auto_prioritize=auto_prioritize,
        max_retries=max_retries,
        protect_tests=protect_tests,
        test_patterns=[p.strip() for p in test_patterns.split(",") if p.strip()],
    )
