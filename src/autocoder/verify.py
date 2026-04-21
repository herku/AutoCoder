from __future__ import annotations

import subprocess
import time

from autocoder.types import RunConfig, VerifyResult


def run_verification(cfg: RunConfig) -> list[VerifyResult]:
    results: list[VerifyResult] = []

    steps = [
        ("lint", cfg.lint_cmd),
        ("unit", cfg.test_cmd),
        ("integration", cfg.integration_cmd),
        ("build", cfg.build_cmd),
    ]

    for stage, cmd in steps:
        if not cmd:
            continue
        result = _run_step(cmd, cfg.repo_path, stage)
        results.append(result)
        if not result.passed:
            break  # Stop on first failure

    return results


def _run_step(cmd: str, repo_path: str, stage: str) -> VerifyResult:
    start = time.monotonic()
    result = subprocess.run(
        cmd,
        shell=True,
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=300,  # 5 minute timeout per step
    )
    duration_ms = int((time.monotonic() - start) * 1000)

    return VerifyResult(
        passed=result.returncode == 0,
        stage=stage,
        exit_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        duration_ms=duration_ms,
    )


def format_failure(result: VerifyResult) -> str:
    output = result.stderr or result.stdout
    lines = output.strip().split("\n")
    # Keep last 200 lines to avoid context overflow
    if len(lines) > 200:
        lines = lines[-200:]
    truncated = "\n".join(lines)
    return f"{result.stage} failed (exit code {result.exit_code}):\n{truncated}"
