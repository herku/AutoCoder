from __future__ import annotations

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

    if cfg.lint_cmd:
        tools.append(f"Bash({cfg.lint_cmd})")
    if cfg.test_cmd:
        tools.append(f"Bash({cfg.test_cmd})")
    if cfg.integration_cmd:
        tools.append(f"Bash({cfg.integration_cmd})")

    # Common safe commands
    tools.extend([
        "Bash(npm:*)",
        "Bash(npx:*)",
        "Bash(python:*)",
        "Bash(pip:*)",
        "Bash(make:*)",
        "Bash(cargo:*)",
        "Bash(go:*)",
    ])

    return SandboxConfig(allowed_tools=tools, docker=cfg.docker)


def build_claude_cmd(
    prompt: str,
    model: str,
    max_budget_usd: float,
    sandbox: SandboxConfig,
    repo_path: str,
) -> list[str]:
    cmd = [
        "claude", "-p",
        "--model", model,
        "--output-format", "json",
        "--max-budget-usd", str(max_budget_usd),
    ]

    for tool in sandbox.allowed_tools:
        cmd.extend(["--allowedTools", tool])

    cmd.append(prompt)

    if sandbox.docker:
        docker_cmd = [
            "docker", "run", "--rm",
            "-v", f"{repo_path}:/workspace",
            "-w", "/workspace",
            "--network=none",
            "-e", "ANTHROPIC_API_KEY",
            sandbox.docker_image,
        ] + cmd
        return docker_cmd

    return cmd
