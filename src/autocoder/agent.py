from __future__ import annotations

import json
import subprocess
import time

from autocoder.issues import truncate_body
from autocoder.sandbox import SandboxConfig, build_claude_cmd
from autocoder.types import AgentResult, AgentError, Issue

PROMPT_BODY_MAX = 4000


def build_prompt(issue: Issue, error_context: str = "", repo_path: str = "", triage_model: str = "") -> str:
    body = truncate_body(issue.body, PROMPT_BODY_MAX)

    parts = [
        f"Fix GitHub issue #{issue.number}: {issue.title}\n",
        f"Issue body:\n{body}\n",
        "Instructions:",
        "- Read relevant source files before making changes",
        "- Write or update tests that verify your fix",
        "- Do NOT modify existing test assertions unless the issue specifically requires it",
        "- Do NOT delete or comment out existing tests",
        "- Run the test suite to verify your changes work",
        "- Keep changes minimal and focused on the issue",
    ]

    if error_context:
        parts.extend([
            "\n--- PREVIOUS ATTEMPT FAILED ---",
            "The previous attempt to fix this issue failed with these errors.",
            "Try a DIFFERENT approach this time:\n",
            error_context,
            "--- END PREVIOUS ERRORS ---",
        ])

    return "\n".join(parts)


def invoke_agent(
    prompt: str,
    repo_path: str,
    model: str,
    effort: str,
    max_budget_usd: float,
    sandbox: SandboxConfig,
) -> AgentResult:
    cmd = build_claude_cmd(model, effort, max_budget_usd, sandbox, repo_path)

    start = time.monotonic()
    result = subprocess.run(
        cmd,
        input=prompt,
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,  # 10 minute hard timeout
    )
    duration_ms = int((time.monotonic() - start) * 1000)

    # Try to parse JSON even on non-zero exit — Claude returns exit 1
    # with valid JSON for soft errors (permission denied, tool blocked, etc.)
    if result.returncode != 0:
        try:
            data = json.loads(result.stdout)
            # Valid JSON response — parse it normally, is_error will be handled downstream
            return _parse_agent_output(result.stdout, duration_ms, model)
        except (json.JSONDecodeError, ValueError):
            raise AgentError(
                f"Claude CLI exited with code {result.returncode}: "
                f"{result.stderr[:500] or result.stdout[:500]}"
            )

    return _parse_agent_output(result.stdout, duration_ms, model)


def _parse_agent_output(raw: str, duration_ms: int, model: str) -> AgentResult:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Non-JSON output — treat as plain text result
        return AgentResult(
            session_id="unknown",
            result_text=raw[:2000],
            is_error=False,
            duration_ms=duration_ms,
            tokens_in=0,
            tokens_out=0,
            tokens_cached=0,
            cost_usd=0.0,
            num_turns=0,
            model=model,
        )

    # Claude JSON output format
    is_error = data.get("is_error", False)
    result_text = data.get("result", "") or ""
    if isinstance(result_text, list):
        # Result can be a list of content blocks
        result_text = "\n".join(
            block.get("text", "") for block in result_text if block.get("type") == "text"
        )

    usage = data.get("usage", {})

    return AgentResult(
        session_id=data.get("session_id", "unknown"),
        result_text=result_text,
        is_error=is_error,
        duration_ms=duration_ms,
        tokens_in=usage.get("input_tokens", 0),
        tokens_out=usage.get("output_tokens", 0),
        tokens_cached=usage.get("cache_read_input_tokens", 0),
        cost_usd=data.get("cost_usd", 0.0),
        num_turns=data.get("num_turns", 0),
        model=model,
    )
