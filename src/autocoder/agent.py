from __future__ import annotations

import json
import subprocess
import threading
import time

import re

from autocoder.issues import truncate_body
from autocoder.prompts import load
from autocoder.sandbox import SandboxConfig, build_claude_cmd
from autocoder.types import (
    AgentResult, AgentError, AuthenticationError, IdleTimeoutError,
    ImplementerStatus, RateLimitError, Issue, action_verb,
)

PROMPT_BODY_MAX = 4000

_STATUS_RE = re.compile(
    r"^[\s>*\-]*STATUS:\s*<?\s*(DONE_WITH_CONCERNS|NEEDS_CONTEXT|BLOCKED|DONE)\b[\s:>\-]*(.*)$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_status(result_text: str) -> tuple[ImplementerStatus | None, str | None]:
    """Extract the LAST STATUS: <token> line from the agent's report.

    Returns (status, detail) or (None, None) if absent. Detail is the trailing
    text on the same line; full surrounding lines are not captured (the
    implementer is told to put the explanation right after the colon).
    """
    if not result_text:
        return None, None
    matches = list(_STATUS_RE.finditer(result_text))
    if not matches:
        return None, None
    last = matches[-1]
    token = last.group(1).upper()
    detail = (last.group(2) or "").strip() or None
    try:
        return ImplementerStatus(token), detail
    except ValueError:
        return None, None

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


def _brief_block(brief: str) -> str:
    if not brief:
        return ""
    return (
        "\n\nDesign brief from advisory agents (stay inside its Scope decisions"
        " — do NOT implement anything marked OUT):\n"
        f"{brief}\n"
    )


def format_commands_block(
    build_cmd: str | None,
    test_cmd: str | None,
    lint_cmd: str | None,
    integration_cmd: str | None = None,
) -> str:
    """Render the project's configured verification commands for the implementer.

    The templates tell the agent to run "the project's lint/test/build command";
    without this block it has to re-derive commands the orchestrator already
    knows. Returns "" when nothing is configured.
    """
    lines = [
        f"- {label}: `{cmd}`"
        for label, cmd in (
            ("Lint", lint_cmd),
            ("Tests", test_cmd),
            ("Integration tests", integration_cmd),
            ("Build", build_cmd),
        )
        if cmd
    ]
    if not lines:
        return ""
    return (
        "\n## Project commands (use these exact commands to verify)\n"
        + "\n".join(lines)
        + "\n"
    )


DISCUSSION_MAX_COMMENTS = 10
DISCUSSION_COMMENT_MAX = 500
DISCUSSION_BLOCK_MAX = 3000


def format_discussion_block(comments: list[str]) -> str:
    """Render issue comments for the implementer (latest comments kept —
    the tail of a discussion is where clarifications and final decisions
    live). Returns "" when there are none."""
    if not comments:
        return ""
    kept = comments[-DISCUSSION_MAX_COMMENTS:]
    lines = [f"- {c[:DISCUSSION_COMMENT_MAX]}" for c in kept]
    block = (
        "\n## Issue discussion (may contain clarifications that supersede the body)\n"
        + "\n".join(lines)
        + "\n"
    )
    return block[:DISCUSSION_BLOCK_MAX]


CRITERIA_BLOCK_MAX = 6000


def _criteria_block(issue_body: str) -> str:
    """Render the FULL acceptance-criteria list from the untruncated issue body.

    The body shown in the prompt is capped at PROMPT_BODY_MAX, but verification
    checks criteria from the full body — so criteria past the truncation point
    must still reach the implementer.
    """
    from autocoder.testplan import extract_acceptance_criteria

    criteria = extract_acceptance_criteria(issue_body)
    if not criteria:
        return ""
    listing = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(criteria))
    block = (
        "\n## Acceptance criteria (complete list)\n"
        "Verification will check EVERY item below against your change. The issue\n"
        "body above may be truncated — this list is authoritative.\n"
        f"{listing}\n"
    )
    return block[:CRITERIA_BLOCK_MAX]


def build_prompt(issue: Issue, error_context: str = "", repo_path: str = "", triage_model: str = "", plan_mode: bool = False, brief: str = "", commands_block: str = "", discussion_block: str = "") -> str:
    body = truncate_body(issue.body, PROMPT_BODY_MAX)
    verb = action_verb(issue)
    base = load("implement", repo_path or None).format(
        verb=verb,
        issue_number=issue.number,
        issue_title=issue.title,
        body=body,
        acceptance_criteria_block=_criteria_block(issue.body),
        commands_block=commands_block,
        discussion_block=discussion_block,
        error_context_block=_error_block(error_context),
    )
    return base + _brief_block(brief)


def build_plan_prompt(issue: Issue, repo_path: str = "") -> str:
    """Build a prompt for the planning phase (read-only analysis)."""
    body = truncate_body(issue.body, PROMPT_BODY_MAX)
    return load("plan", repo_path or None).format(
        issue_number=issue.number,
        issue_title=issue.title,
        body=body,
    )


def build_implement_prompt(issue: Issue, plan_text: str, error_context: str = "", repo_path: str = "", brief: str = "", commands_block: str = "", discussion_block: str = "") -> str:
    """Build a prompt for the implementation phase, with plan as context."""
    body = truncate_body(issue.body, PROMPT_BODY_MAX)
    verb = action_verb(issue)
    base = load("implement_with_plan", repo_path or None).format(
        verb=verb,
        issue_number=issue.number,
        issue_title=issue.title,
        body=body,
        acceptance_criteria_block=_criteria_block(issue.body),
        commands_block=commands_block,
        discussion_block=discussion_block,
        plan_text=plan_text,
        error_context_block=_error_block(error_context, "The previous attempt failed with these errors."),
    )
    return base + _brief_block(brief)


def build_task_plan_prompt(
    issue: Issue, plan_path: str, brief: str = "", repo_path: str = "",
) -> str:
    """Build the orchestrator prompt that writes the per-issue task plan file."""
    body = truncate_body(issue.body, PROMPT_BODY_MAX)
    return load("task_plan", repo_path or None).format(
        issue_number=issue.number,
        issue_title=issue.title,
        body=body,
        plan_path=plan_path,
        brief_block=_brief_block(brief),
    )


def build_task_execute_prompt(
    issue: Issue, plan_path: str, task_text: str,
    error_context: str = "", repo_path: str = "", commands_block: str = "",
    discussion_block: str = "",
) -> str:
    """Build the prompt for a single-task fresh-session executor."""
    body = truncate_body(issue.body, PROMPT_BODY_MAX)
    verb = action_verb(issue)
    return load("task_execute", repo_path or None).format(
        verb=verb,
        issue_number=issue.number,
        issue_title=issue.title,
        body=body,
        acceptance_criteria_block=_criteria_block(issue.body),
        commands_block=commands_block,
        discussion_block=discussion_block,
        plan_path=plan_path,
        task_text=task_text,
        error_context_block=_error_block(error_context),
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
TIMEOUT_VERIFY_FIX = 600  # 10 minutes for lint/test fix attempt (suites run slower than builds)
TIMEOUT_CLAUDE_MD = 600  # 10 minutes for CLAUDE.md update
TIMEOUT_BRIEF = 600  # 10 minutes for pre-implement brief (3 parallel advisors)
BUDGET_CLAUDE_MD = 2.00  # $2.00 max for doc update


def build_brief_prompt(issue: Issue, repo_path: str = "") -> str:
    """Build the orchestrator prompt for the pre-implement design brief."""
    body = truncate_body(issue.body, PROMPT_BODY_MAX)
    return load("implement_brief", repo_path or None).format(
        issue_number=issue.number,
        issue_title=issue.title,
        body=body,
    )

CLAUDE_MD_DIFF_MAX = 30_000


def generate_implement_brief(
    issue: Issue,
    repo_path: str,
    model: str,
    effort: str,
    max_budget_usd: float,
    sandbox: SandboxConfig,
) -> AgentResult:
    """Run the pre-implement brief orchestrator; returns AgentResult whose
    result_text is the synthesized design brief to prepend to the implementer's
    prompt."""
    prompt = build_brief_prompt(issue, repo_path)
    return invoke_agent(
        prompt, repo_path, model, effort, max_budget_usd, sandbox,
        timeout=TIMEOUT_BRIEF,
    )


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

_idle_timeout_seconds: int | None = None
_session_timeout_seconds: int | None = None
_POLL_INTERVAL_S = 1.0
_TERMINATE_GRACE_S = 5.0


def set_rate_limit_wait(seconds: int | None) -> None:
    """Configure whether invoke_agent should sleep+retry on RateLimitError."""
    global _rate_limit_wait_seconds
    _rate_limit_wait_seconds = seconds


def set_timeouts(idle_seconds: int | None, session_seconds: int | None) -> None:
    """Configure subprocess idle and session hard caps. None disables each."""
    global _idle_timeout_seconds, _session_timeout_seconds
    _idle_timeout_seconds = idle_seconds
    _session_timeout_seconds = session_seconds


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
    returncode, stdout, stderr = _run_with_watchdog(
        cmd, prompt, repo_path, timeout,
        idle_seconds=_idle_timeout_seconds,
        session_seconds=_session_timeout_seconds,
    )
    duration_ms = int((time.monotonic() - start) * 1000)

    # Try to parse JSON even on non-zero exit — Claude returns exit 1
    # with valid JSON for soft errors (permission denied, tool blocked, etc.)
    if returncode != 0:
        try:
            json.loads(stdout)
            parsed = _parse_agent_output(stdout, duration_ms, model)
            if parsed.is_error and _is_rate_limited(parsed.result_text):
                raise RateLimitError(parsed.result_text[:500])
            if parsed.is_error and _is_auth_error(parsed.result_text):
                raise AuthenticationError(parsed.result_text[:500])
            return parsed
        except (json.JSONDecodeError, ValueError):
            combined = f"{stderr} {stdout}"
            msg = (
                f"Claude CLI exited with code {returncode}: "
                f"{stderr[:500] or stdout[:500]}"
            )
            if _is_rate_limited(combined):
                raise RateLimitError(msg)
            if _is_auth_error(combined):
                raise AuthenticationError(msg)
            raise AgentError(msg)

    parsed = _parse_agent_output(stdout, duration_ms, model)
    if parsed.is_error and _is_rate_limited(parsed.result_text):
        raise RateLimitError(parsed.result_text[:500])
    if parsed.is_error and _is_auth_error(parsed.result_text):
        raise AuthenticationError(parsed.result_text[:500])
    return parsed


def _run_with_watchdog(
    cmd: list[str],
    prompt: str,
    repo_path: str,
    wall_timeout: int,
    idle_seconds: int | None,
    session_seconds: int | None,
) -> tuple[int, str, str]:
    """Run a subprocess with an optional idle/session watchdog.

    Always uses Popen + reader threads so idle-activity can be detected from
    stdout. wall_timeout still fires as subprocess.TimeoutExpired to preserve
    the existing timeout contract. idle_seconds and session_seconds raise
    IdleTimeoutError when breached. session_seconds, when set below
    wall_timeout, fires first and is the effective hard cap.
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=repo_path,
        bufsize=1,  # line-buffered so readline unblocks promptly
    )

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    state = {"last_byte_ts": time.monotonic()}
    state_lock = threading.Lock()

    def _read(stream, buf: list[str]) -> None:
        try:
            for line in iter(stream.readline, ""):
                with state_lock:
                    state["last_byte_ts"] = time.monotonic()
                buf.append(line)
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _write() -> None:
        try:
            if proc.stdin is not None:
                proc.stdin.write(prompt)
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    t_out = threading.Thread(target=_read, args=(proc.stdout, stdout_chunks), daemon=True)
    t_err = threading.Thread(target=_read, args=(proc.stderr, stderr_chunks), daemon=True)
    t_in = threading.Thread(target=_write, daemon=True)
    t_out.start()
    t_err.start()
    t_in.start()

    start = time.monotonic()
    try:
        while True:
            try:
                returncode = proc.wait(timeout=_POLL_INTERVAL_S)
                break
            except subprocess.TimeoutExpired:
                pass

            now = time.monotonic()
            elapsed = now - start

            if elapsed > wall_timeout:
                _kill_proc(proc)
                raise subprocess.TimeoutExpired(cmd, wall_timeout)

            if session_seconds is not None and elapsed > session_seconds:
                _kill_proc(proc)
                raise IdleTimeoutError(
                    f"Session timeout: ran for {int(elapsed)}s (limit {session_seconds}s)"
                )

            if idle_seconds is not None:
                with state_lock:
                    idle_for = now - state["last_byte_ts"]
                if idle_for > idle_seconds:
                    _kill_proc(proc)
                    raise IdleTimeoutError(
                        f"Idle timeout: no output for {int(idle_for)}s (limit {idle_seconds}s)"
                    )
    finally:
        t_out.join(timeout=2.0)
        t_err.join(timeout=2.0)
        t_in.join(timeout=2.0)

    return returncode, "".join(stdout_chunks), "".join(stderr_chunks)


def _kill_proc(proc: subprocess.Popen) -> None:
    """SIGTERM, wait up to _TERMINATE_GRACE_S, then SIGKILL."""
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=_TERMINATE_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=_TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired:
        pass


def _parse_agent_output(raw: str, duration_ms: int, model: str) -> AgentResult:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Non-JSON output — treat as plain text result
        plain = raw[:2000]
        status, status_detail = parse_status(plain)
        return AgentResult(
            session_id="unknown",
            result_text=plain,
            is_error=False,
            duration_ms=duration_ms,
            tokens_in=0,
            tokens_out=0,
            tokens_cached=0,
            cost_usd=0.0,
            num_turns=0,
            model=model,
            status=status,
            status_detail=status_detail,
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
    status, status_detail = parse_status(result_text)

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
        status=status,
        status_detail=status_detail,
    )
