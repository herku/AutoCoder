from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field

from autocoder.types import RunConfig


@dataclass
class SandboxConfig:
    allowed_tools: list[str]
    docker: bool
    docker_image: str = "autocoder-sandbox"


def build_sandbox(cfg: RunConfig) -> SandboxConfig:
    tools: list[str] = [
        "Edit",
        "Read",
        "Glob",
        "Grep",
        "Write",
        "Bash(git diff:*)",
        "Bash(git status:*)",
        "Bash(git add:*)",
        "Bash(git log:*)",
    ]

    # Allow the exact command plus a full-command prefix wildcard so agents
    # can run targeted subsets (e.g. `uv run pytest tests/test_x.py::test_y`).
    # The wildcard is anchored to the whole configured command, never its
    # first token — `Bash(uv:*)` would authorize far more than intended.
    for cmd in (cfg.build_cmd, cfg.lint_cmd, cfg.test_cmd, cfg.integration_cmd):
        if cmd:
            tools.append(f"Bash({cmd})")
            tools.append(f"Bash({cmd}:*)")

    # Common safe commands
    tools.extend([
        "Bash(npm:*)",
        "Bash(npx:*)",
        "Bash(python:*)",
        "Bash(pip:*)",
        "Bash(uv:*)",
        "Bash(make:*)",
        "Bash(cargo:*)",
        "Bash(go:*)",
        "Bash(swift:*)",
        "Bash(xcodebuild:*)",
    ])

    return SandboxConfig(allowed_tools=tools, docker=cfg.docker)


def build_plan_sandbox(cfg: RunConfig) -> SandboxConfig:
    """Build a read-only sandbox for the planning phase."""
    tools: list[str] = [
        "Read",
        "Glob",
        "Grep",
        "Bash(git diff:*)",
        "Bash(git status:*)",
        "Bash(git log:*)",
    ]
    return SandboxConfig(allowed_tools=tools, docker=cfg.docker)


def build_claude_md_sandbox(cfg: RunConfig) -> SandboxConfig:
    """Build a tightly scoped sandbox for CLAUDE.md updates."""
    tools: list[str] = [
        "Read",
        "Edit",
        "Write",
        "Glob",
        "Grep",
        "Bash(git diff:*)",
        "Bash(git status:*)",
    ]
    return SandboxConfig(allowed_tools=tools, docker=cfg.docker)


def build_review_sandbox(cfg: RunConfig) -> SandboxConfig:
    """Build a sandbox for the multi-agent review orchestrator.

    Needs the same write capabilities as the main sandbox (the orchestrator
    fixes issues in-session) plus explicit Task whitelisting so it can spawn
    parallel sub-agents.
    """
    base = build_sandbox(cfg)
    tools = list(base.allowed_tools)
    if "Task" not in tools:
        tools.append("Task")
    return SandboxConfig(allowed_tools=tools, docker=base.docker)


def build_brief_sandbox(cfg: RunConfig) -> SandboxConfig:
    """Build a read-only sandbox for the pre-implement brief orchestrator.

    Needs Read/Glob/Grep + Task to spawn parallel advisors, plus git history
    commands. No write tools — the brief phase produces text, not code.
    """
    tools: list[str] = [
        "Read",
        "Glob",
        "Grep",
        "Task",
        "Bash(git diff:*)",
        "Bash(git status:*)",
        "Bash(git log:*)",
    ]
    return SandboxConfig(allowed_tools=tools, docker=cfg.docker)


def build_detect_sandbox(cfg: RunConfig) -> SandboxConfig:
    """Build a read-only sandbox for AI build command detection."""
    tools: list[str] = [
        "Read",
        "Glob",
        "Grep",
        "Bash(git diff:*)",
        "Bash(git status:*)",
        "Bash(git log:*)",
    ]
    return SandboxConfig(allowed_tools=tools, docker=cfg.docker)


def build_claude_cmd(
    model: str,
    effort: str,
    max_budget_usd: float,
    sandbox: SandboxConfig,
    repo_path: str,
) -> list[str]:
    """Build claude CLI command. Prompt is passed via stdin, not as an arg."""
    cmd = [
        "claude", "-p",
        "--model", model,
        "--effort", effort,
        "--output-format", "json",
        "--max-budget-usd", str(max_budget_usd),
    ]

    for tool in sandbox.allowed_tools:
        cmd.extend(["--allowedTools", tool])

    if sandbox.docker:
        home = os.path.expanduser("~")
        oauth = _get_oauth_tokens()
        docker_cmd = [
            "docker", "run", "--rm", "-i",
            "-v", f"{repo_path}:/workspace",
            "-v", f"{home}/.claude:/home/node/.claude:ro",
            "-v", f"{home}/.claude.json:/home/node/.claude.json:ro",
            "-w", "/workspace",
        ]
        if oauth:
            docker_cmd.extend([
                "-e", f"CLAUDE_CODE_OAUTH_TOKEN={oauth['accessToken']}",
                "-e", f"CLAUDE_CODE_OAUTH_REFRESH_TOKEN={oauth['refreshToken']}",
            ])
        docker_cmd.append(sandbox.docker_image)
        docker_cmd.extend(cmd)
        return docker_cmd

    return cmd


def _get_oauth_tokens() -> dict | None:
    """Extract Claude OAuth tokens from macOS keychain."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, check=True,
        )
        creds = json.loads(result.stdout.strip())
        oauth = creds.get("claudeAiOauth", {})
        if oauth.get("accessToken") and oauth.get("refreshToken"):
            return oauth
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError):
        pass
    return None
