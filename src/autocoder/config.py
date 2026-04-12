from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from autocoder.types import RunConfig


def build_config(
    repo: str,
    labels: str | None,
    test_cmd: str | None,
    lint_cmd: str | None,
    integration_cmd: str | None,
    model: str,
    plan_model: str,
    review_model: str,
    effort: str,
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
    auto_merge: bool,
    force_prioritize: bool = False,
    update_docker: bool = False,
    docker_max_age_days: int = 7,
    issues: tuple[int, ...] = (),
    plan_mode: bool = False,
    update_claude_md: bool = True,
    ci_timeout: int = 1800,
) -> RunConfig:
    repo_path = str(Path(repo).resolve())

    git_dir = Path(repo_path) / ".git"
    if not git_dir.exists():
        raise SystemExit(f"Error: {repo_path} is not a git repository")

    if update_docker and not docker:
        print("Warning: --update-docker has no effect without --docker. Ignoring.")

    if docker:
        image_name = "autocoder-sandbox"
        result = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise SystemExit(
                "Error: --docker is enabled but Docker is not running.\n"
                "Start Docker Desktop or remove --docker to run without sandboxing."
            )

        needs_build = False
        if update_docker:
            print(f"--update-docker: forcing rebuild of '{image_name}' image...")
            needs_build = True
        else:
            result = subprocess.run(
                ["docker", "image", "inspect", image_name],
                capture_output=True, text=True, check=False,
            )
            if result.returncode != 0:
                print(f"Docker image '{image_name}' not found. Building...")
                needs_build = True
            else:
                try:
                    inspect_data = json.loads(result.stdout)
                    created_str = inspect_data[0]["Created"]
                    # Docker returns ISO 8601 with nanosecond precision
                    # Truncate to microseconds for datetime.fromisoformat()
                    created_str = created_str.replace("Z", "+00:00")
                    if "." in created_str:
                        dot_pos = created_str.index(".")
                        plus_pos = created_str.index("+", dot_pos)
                        frac = created_str[dot_pos:plus_pos]
                        if len(frac) > 7:  # .XXXXXX = 7 chars max
                            frac = frac[:7]
                        created_str = created_str[:dot_pos] + frac + created_str[plus_pos:]
                    created = datetime.fromisoformat(created_str)
                    age_days = (datetime.now(timezone.utc) - created).days
                    if age_days >= docker_max_age_days:
                        print(
                            f"Docker image '{image_name}' is {age_days} days old "
                            f"(threshold: {docker_max_age_days}). Rebuilding..."
                        )
                        needs_build = True
                except (json.JSONDecodeError, KeyError, IndexError, ValueError):
                    pass

        if needs_build:
            dockerfile = Path(__file__).resolve().parent.parent.parent / "Dockerfile"
            if not dockerfile.exists():
                raise SystemExit(
                    f"Error: '{image_name}' image needs rebuild but no Dockerfile available.\n"
                    "Remove --docker to run without sandboxing."
                )
            build = subprocess.run(
                ["docker", "build", "--no-cache", "-t", image_name, str(dockerfile.parent)],
                check=False,
            )
            if build.returncode != 0:
                raise SystemExit(
                    f"Error: Failed to build '{image_name}' Docker image.\n"
                    "Remove --docker to run without sandboxing."
                )
        # Check host auth files exist
        home = Path.home()
        if not (home / ".claude.json").exists():
            raise SystemExit(
                "Error: ~/.claude.json not found. Claude is not logged in.\n"
                "Run 'claude login' first, then retry with --docker."
            )

    issue_numbers = list(issues)

    return RunConfig(
        repo_path=repo_path,
        labels=[l.strip() for l in labels.split(",") if l.strip()] if labels else [],
        test_cmd=test_cmd,
        lint_cmd=lint_cmd,
        integration_cmd=integration_cmd,
        model=model,
        plan_model=plan_model,
        review_model=review_model,
        effort=effort,
        triage_model=triage_model,
        max_issues=max_issues,
        max_analyze=max_analyze,
        max_turns=max_turns,
        token_budget=token_budget,
        daily_cap=daily_cap,
        docker=docker,
        update_docker=update_docker,
        docker_max_age_days=docker_max_age_days,
        log_dir=log_dir,
        dry_run=dry_run,
        auto_prioritize=False if issue_numbers else auto_prioritize,
        max_retries=max_retries,
        protect_tests=protect_tests,
        test_patterns=[p.strip() for p in test_patterns.split(",") if p.strip()],
        auto_merge=auto_merge,
        force_prioritize=force_prioritize,
        plan_mode=plan_mode,
        issue_numbers=issue_numbers,
        update_claude_md=update_claude_md,
        ci_timeout=ci_timeout,
    )
