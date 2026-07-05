from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Outcome(str, Enum):
    SUCCESS = "success"
    RETRY = "retry"
    SKIP = "skip"


class Priority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class ImplementerStatus(str, Enum):
    DONE = "DONE"
    DONE_WITH_CONCERNS = "DONE_WITH_CONCERNS"
    BLOCKED = "BLOCKED"
    NEEDS_CONTEXT = "NEEDS_CONTEXT"


@dataclass
class Issue:
    number: int
    title: str
    body: str
    labels: list[str]
    priority: Priority
    url: str


@dataclass
class AgentResult:
    session_id: str
    result_text: str
    is_error: bool
    duration_ms: int
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    cost_usd: float
    num_turns: int
    model: str
    status: Optional["ImplementerStatus"] = None
    status_detail: Optional[str] = None


@dataclass
class VerifyResult:
    passed: bool
    stage: str  # "lint", "unit", "integration"
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


@dataclass
class RunConfig:
    repo_path: str
    labels: list[str]
    test_cmd: Optional[str]
    lint_cmd: Optional[str]
    integration_cmd: Optional[str]
    model: str
    plan_model: str
    review_model: str
    effort: str
    triage_model: str
    max_issues: int
    max_analyze: int
    max_turns: int
    token_budget: int
    daily_cap: int
    docker: bool
    log_dir: str
    dry_run: bool
    auto_prioritize: bool
    max_retries: int
    protect_tests: bool
    test_patterns: list[str]
    auto_merge: bool
    plan_mode: bool
    issue_numbers: list[int] = field(default_factory=list)
    build_cmd: Optional[str] = None
    build_retries: int = 1
    update_claude_md: bool = True
    force_prioritize: bool = False
    update_docker: bool = False
    docker_max_age_days: int = 7
    ci_timeout: int = 1800
    rate_limit_wait_seconds: Optional[int] = None
    stalemate_threshold: int = 2
    review_mode: str = "single"
    review_budget_usd: float = 2.00
    external_reviewer_cmd: Optional[list[str]] = None
    implement_brief: bool = True
    brief_budget_usd: float = 1.00
    pre_verify_critique: bool = True
    pre_verify_budget_usd: float = 1.50
    idle_timeout_seconds: Optional[int] = None
    session_timeout_seconds: Optional[int] = None
    serve: bool = False
    port: int = 8765
    task_slice: Optional[bool] = None  # None = auto-heuristic
    task_retries: int = 1
    max_tasks: int = 15
    parallel: int = 1
    worktree_root: Optional[str] = None
    escalate_on_block: bool = True
    escalation_model: str = "claude-opus-4-8"
    ci_arch_review: bool = True


@dataclass
class ReviewFinding:
    severity: str  # "critical", "medium"
    file: str
    description: str


@dataclass
class ReviewResult:
    findings: list[ReviewFinding]
    raw_response: str
    has_actionable_issues: bool


@dataclass
class MultiReviewResult:
    """Result from a multi-agent orchestrator review that fixes in-session."""
    cleaned: bool  # True if REVIEW_DONE (nothing needed) or REVIEW_FIXED
    failed: bool  # True if REVIEW_FAILED (unfixable critical issues)
    summary: str  # Free-form summary (final signal line + optional reason)
    raw_response: str


@dataclass
class PlanCheckItem:
    criterion: str
    status: str  # "pass" or "fail"
    evidence: str


@dataclass
class TestPlanResult:
    items: list[PlanCheckItem]
    raw_response: str
    all_passed: bool


@dataclass
class CIResult:
    passed: bool
    output: str
    timed_out: bool


@dataclass
class TaskItem:
    index: int  # 1-based position in plan
    text: str
    done: bool


class AgentError(Exception):
    pass


class RateLimitError(AgentError):
    """Claude CLI hit API rate limit — retrying is futile until reset."""
    pass


class AuthenticationError(AgentError):
    """OAuth token expired or invalid — retrying is futile until re-authenticated."""
    pass


class IdleTimeoutError(AgentError):
    """Claude subprocess produced no output for the configured idle window, or
    exceeded the configured session cap. Retryable — treat as a silent hang."""
    pass


class ImplementerBlockedError(AgentError):
    """Implementer reported STATUS: BLOCKED (or NEEDS_CONTEXT) and the
    in-attempt escalation retry also failed."""
    pass


class VerificationError(Exception):
    def __init__(self, stage: str, output: str):
        self.stage = stage
        self.output = output
        super().__init__(f"{stage} failed: {output[:200]}")


class AntiCheatViolation(Exception):
    pass


class LockError(Exception):
    pass


@dataclass
class EpicResult:
    epic_number: int
    sub_issues: list[int]
    succeeded: list[int]
    failed: list[int]
    skipped_closed: list[int]
    all_complete: bool


_BUG_LABELS = {"bug", "bugfix", "defect", "regression", "error", "crash"}
_EPIC_LABELS = {"epic", "meta", "tracking"}


def commit_prefix(issue: Issue) -> str:
    """Return conventional commit prefix based on issue labels."""
    lower_labels = {label.lower() for label in issue.labels}
    if lower_labels & _BUG_LABELS:
        return "fix"
    return "feat"


def action_verb(issue: Issue) -> str:
    """Return action verb for prompts based on issue type."""
    lower_labels = {label.lower() for label in issue.labels}
    if lower_labels & _BUG_LABELS:
        return "Fix"
    return "Implement"


def is_epic(issue: Issue) -> bool:
    """Return True if the issue is an epic/meta/tracking issue."""
    lower_labels = {label.lower() for label in issue.labels}
    return bool(lower_labels & _EPIC_LABELS)
