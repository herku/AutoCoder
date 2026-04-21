from __future__ import annotations

import json
import subprocess
import time

from autocoder.issues import truncate_body
from autocoder.prompts import load
from autocoder.sandbox import SandboxConfig, build_claude_cmd
from autocoder.types import AgentResult, AgentError, AuthenticationError, RateLimitError, Issue, action_verb

PROMPT_BODY_MAX = 4000

_RATE_LIMIT_PATTERNS = [
    "hit your limit",
    "rate limit",
    "rate_limit",
    "too many requests",
    "quota exceeded",
]

_AUTH_ERROR_PATTERNS = [
    "authentication_error",
    "token has expired",
    "failed to authenticate",
    "unauthorized",
]


def _is_rate_limited(text: str) -> bool:
    """Check if text contains rate limit indicators."""
    lower = text.lower()
    return any(p in lower for p in _RATE_LIMIT_PATTERNS)


def _is_auth_error(text: str) -> bool:
    """Check if text contains authentication failure indicators."""
    lower = text.lower()
    return any(p in lower for p in _AUTH_ERROR_PATTERNS)


def _error_block(ctx: str, message: str = "The previous attempt to fix this issue failed with these errors.") -> str:
    if not ctx:
        return ""
    return (
        "\n\n--- PREVIOUS ATTEMPT FAILED ---\n"
        f"{message}\n"
        "Try a DIFFERENT approach this time:\n\n"
        f"{ctx}\n"
        "--- END PREVIOUS ERRORS ---"
    )


def build_prompt(issue: Issue, error_context: str = "", repo_path: str = "", triage_model: str = "", plan_mode: bool = False) -> str:
    body = truncate_body(issue.body, PROMPT_BODY_MAX)
    verb = action_verb(issue)
    return load("implement", repo_path or None).format(
        verb=verb,
        issue_number=issue.number,
        issue_title=issue.title,
        body=body,
        error_context_block=_error_block(error_context),
    )


def build_plan_prompt(issue: Issue, repo_path: str = "") -> str:
    """Build a prompt for the planning phase (read-only analysis)."""
    body = truncate_body(issue.body, PROMPT_BODY_MAX)
    return load("plan", repo_path or None).format(
        issue_number=issue.number,
        issue_title=issue.title,
        body=body,
    )


def build_implement_prompt(issue: Issue, plan_text: str, error_context: str = "", repo_path: str = "") -> str:
    """Build a prompt for the implementation phase, with plan as context."""
    body = truncate_body(issue.body, PROMPT_BODY_MAX)
    verb = action_verb(issue)
    return load("implement_with_plan", repo_path or None).format(
        verb=verb,
        issue_number=issue.number,
        issue_title=issue.title,
        body=body,
        plan_text=plan_text,
        error_context_block=_error_block(error_context, "The previous attempt failed with these errors."),
    )


BUDGET_CI_LEARN = 1.00  # $1.00 max for CI learning step
BUDGET_IMPL_LEARN = 1.00  # $1.00 max for implementation learning step
CI_LEARN_OUTPUT_MAX = 5_000
CI_LEARN_DIFF_MAX = 5_000
IMPL_LEARN_MAX = 5_000


def build_ci_learn_prompt(ci_output: str, fix_diff: str, repo_path: str = "") -> str:
    """Build a prompt to capture CI fix learnings into the repo's CLAUDE.md."""
    truncated_ci = ci_output[:CI_LEARN_OUTPUT_MAX]
    truncated_diff = fix_diff[:CI_LEARN_DIFF_MAX]
    return load("ci_learn", repo_path or None).format(ci_output=truncated_ci, fix_diff=truncated_diff)


def build_impl_learn_prompt(diff_stats: str, verify_summary: str, repo_path: str = "") -> str:
    """Build a prompt to capture implementation learnings into the repo's CLAUDE.md."""
    truncated_stats = diff_stats[:IMPL_LEARN_MAX]
    truncated_verify = verify_summary[:IMPL_LEARN_MAX]
    return load("impl_learn", repo_path or None).format(diff_stats=truncated_stats, verify_summary=truncated_verify)


TIMEOUT_PLAN = 3600  # 60 minutes for plan phase (read-only analysis)
TIMEOUT_IMPLEMENT = 6000  # 100 minutes for implementation phase
TIMEOUT_BUILD_FIX = 300  # 5 minutes for build fix attempt
TIMEOUT_CLAUDE_MD = 600  # 10 minutes for CLAUDE.md update
BUDGET_CLAUDE_MD = 2.00  # $2.00 max for doc update

CLAUDE_MD_DIFF_MAX = 30_000


def build_update_claude_md_prompt(diff: str, existing_claude_md: str | None, repo_path: str = "") -> str:
    """Build a prompt for updating the repo's CLAUDE.md with architecture info."""
    truncated_diff = diff[:CLAUDE_MD_DIFF_MAX] if len(diff) > CLAUDE_MD_DIFF_MAX else diff
    md_content = existing_claude_md or "(No CLAUDE.md exists yet. Create one from scratch.)"
    return load("update_claude_md", repo_path or None).format(
        existing_claude_md=md_content,
        truncated_diff=truncated_diff,
    )


_rate_limit_wait_seconds: int | None = None
_RATE_LIMIT_MAX_RETRIES = 3


def set_rate_limit_wait(seconds: int | None) -> None:
    """Configure whether invoke_agent should sleep+retry on RateLimitError."""
    global _rate_limit_wait_seconds
    _rate_limit_wait_seconds = seconds


def invoke_agent(
    prompt: str,
    repo_path: str,
    model: str,
    effort: str,
    max_budget_usd: float,
    sandbox: SandboxConfig,
    timeout: int = TIMEOUT_IMPLEMENT,
) -> AgentResult:
    for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
        try:
            return _invoke_once(prompt, repo_path, model, effort, max_budget_usd, sandbox, timeout)
        except RateLimitError:
            if _rate_limit_wait_seconds is None or attempt == _RATE_LIMIT_MAX_RETRIES:
                raise
            print(
                f"  Rate limit hit — waiting {_rate_limit_wait_seconds}s before retry "
                f"({attempt + 1}/{_RATE_LIMIT_MAX_RETRIES})...",
                flush=True,
            )
            time.sleep(_rate_limit_wait_seconds)


def _invoke_once(
    prompt: str,
    repo_path: str,
    model: str,
    effort: str,
    max_budget_usd: float,
    sandbox: SandboxConfig,
    timeout: int,
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
        timeout=timeout,
    )
    duration_ms = int((time.monotonic() - start) * 1000)

    # Try to parse JSON even on non-zero exit — Claude returns exit 1
    # with valid JSON for soft errors (permission denied, tool blocked, etc.)
    if result.returncode != 0:
        try:
            data = json.loads(result.stdout)
            # Valid JSON response — parse it normally, is_error will be handled downstream
            parsed = _parse_agent_output(result.stdout, duration_ms, model)
            if parsed.is_error and _is_rate_limited(parsed.result_text):
                raise RateLimitError(parsed.result_text[:500])
            if parsed.is_error and _is_auth_error(parsed.result_text):
                raise AuthenticationError(parsed.result_text[:500])
            return parsed
        except (json.JSONDecodeError, ValueError):
            combined = f"{result.stderr} {result.stdout}"
            msg = (
                f"Claude CLI exited with code {result.returncode}: "
                f"{result.stderr[:500] or result.stdout[:500]}"
            )
            if _is_rate_limited(combined):
                raise RateLimitError(msg)
            if _is_auth_error(combined):
                raise AuthenticationError(msg)
            raise AgentError(msg)

    parsed = _parse_agent_output(result.stdout, duration_ms, model)
    if parsed.is_error and _is_rate_limited(parsed.result_text):
        raise RateLimitError(parsed.result_text[:500])
    if parsed.is_error and _is_auth_error(parsed.result_text):
        raise AuthenticationError(parsed.result_text[:500])
    return parsed


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
