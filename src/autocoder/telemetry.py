from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from autocoder.types import AgentResult, ReviewResult, TestPlanResult, VerifyResult


class Phase(str, Enum):
    PLAN = "plan"
    IMPLEMENT_BRIEF = "implement_brief"
    IMPLEMENT = "implement"
    PRE_VERIFY_CRITIQUE = "pre_verify_critique"
    REVIEW_FIX = "review_fix"
    REVIEW_MULTI = "review_multi"  # deprecated; kept for backward-compatible log parsing
    REVIEW_SPEC_COMPLIANCE = "review_spec_compliance"
    REVIEW_QUALITY = "review_quality"
    REVIEW_EXTERNAL = "review_external"
    TESTPLAN_FIX = "testplan_fix"
    UPDATE_CLAUDE_MD = "update_claude_md"
    CI_FIX = "ci_fix"
    CI_FIX_ARCH = "ci_fix_arch"
    BUILD_FIX = "build_fix"
    TASK_PLAN = "task_plan"
    TASK_EXEC = "task_exec"


class FailureCategory(str, Enum):
    BUILD_FAIL = "build_fail"
    LINT_FAIL = "lint_fail"
    TEST_FAIL = "test_fail"
    INTEGRATION_FAIL = "integration_fail"
    REVIEW_REJECTED = "review_rejected"
    REVIEW_STALEMATE = "review_stalemate"
    SPEC_COMPLIANCE_FAILED = "spec_compliance_failed"
    QUALITY_REVIEW_FAILED = "quality_review_failed"
    TESTPLAN_FAIL = "testplan_fail"
    RATE_LIMIT = "rate_limit"
    AUTH_ERROR = "auth_error"
    BUDGET_EXHAUSTED = "budget_exhausted"
    AGENT_ERROR = "agent_error"
    ANTICHEAT_VIOLATION = "anticheat_violation"
    TIMEOUT = "timeout"
    CI_FAIL = "ci_fail"
    CI_STALEMATE = "ci_stalemate"
    CI_TIMEOUT = "ci_timeout"
    IDLE_TIMEOUT = "idle_timeout"
    TASK_PLAN_FAIL = "task_plan_fail"
    TASK_EXEC_FAIL = "task_exec_fail"


@dataclass
class PhaseMetrics:
    phase: Phase
    model: str
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    cost_usd: float
    duration_ms: int
    num_turns: int

    @property
    def cache_hit_rate(self) -> float:
        if self.tokens_in == 0:
            return 0.0
        return self.tokens_cached / self.tokens_in


@dataclass
class VerifyStageMetrics:
    stage: str
    passed: bool
    exit_code: int
    duration_ms: int


@dataclass
class IssueTelemetry:
    issue_number: int
    attempt: int
    phases: list[PhaseMetrics] = field(default_factory=list)
    verify_stages: list[VerifyStageMetrics] = field(default_factory=list)
    review_findings: list[dict] = field(default_factory=list)
    testplan_items: list[dict] = field(default_factory=list)
    failure_category: Optional[FailureCategory] = None
    outcome: Optional[str] = None

    @property
    def total_tokens_in(self) -> int:
        return sum(p.tokens_in for p in self.phases)

    @property
    def total_tokens_out(self) -> int:
        return sum(p.tokens_out for p in self.phases)

    @property
    def total_tokens_cached(self) -> int:
        return sum(p.tokens_cached for p in self.phases)

    @property
    def total_cost_usd(self) -> float:
        return sum(p.cost_usd for p in self.phases)

    @property
    def cache_hit_rate(self) -> float:
        total_in = self.total_tokens_in
        if total_in == 0:
            return 0.0
        return self.total_tokens_cached / total_in


@dataclass
class RunSummary:
    issues_processed: int
    success_count: int
    retry_count: int
    skip_count: int
    total_cost_usd: float
    phase_cost_breakdown: dict[str, float]
    phase_token_breakdown: dict[str, int]
    per_model_cost: dict[str, float]
    overall_cache_hit_rate: float
    top_failure_reasons: list[tuple[str, int]]
    # Expanded token fields
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_tokens_cached: int = 0
    daily_tokens_used: int = 0
    daily_cap_tokens: int = 0
    phase_token_detail: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    per_model_tokens: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    per_issue_summary: dict[int, tuple[int, int, int, float]] = field(default_factory=dict)


class Telemetry:
    """Central telemetry collector.

    Thread-safe: each worker thread has its own 'current issue' slot so
    concurrent issues don't clobber one another's PhaseMetrics. Completed
    issues are appended to a shared list under a lock.
    """

    def __init__(self, event_bus: "object | None" = None) -> None:
        self._local = threading.local()
        self._completed: list[IssueTelemetry] = []
        self._lock = threading.Lock()
        self._bus = event_bus

    def _get_current(self) -> Optional[IssueTelemetry]:
        return getattr(self._local, "current", None)

    def _set_current(self, value: Optional[IssueTelemetry]) -> None:
        self._local.current = value

    def _emit(self, event_type: str, data: dict) -> None:
        if self._bus is None:
            return
        try:
            self._bus.publish(event_type, data)  # type: ignore[attr-defined]
        except Exception:
            pass  # bus failures must never break the pipeline

    def emit(self, event_type: str, **data: object) -> None:
        """Public emit for custom events (rate_limit, idle, ci_attempt, ...)."""
        self._emit(event_type, dict(data))

    def begin_issue(self, issue_number: int, attempt: int, title: str = "") -> None:
        current = self._get_current()
        # If a previous issue on this thread wasn't ended (e.g. unexpected
        # exception), flush it now.
        if current is not None:
            with self._lock:
                self._completed.append(current)
        self._set_current(IssueTelemetry(issue_number=issue_number, attempt=attempt))
        self._emit("issue_start", {"issue": issue_number, "attempt": attempt, "title": title})

    def record_phase(self, phase: Phase, agent_result: AgentResult) -> None:
        current = self._get_current()
        if current is None:
            return
        current.phases.append(PhaseMetrics(
            phase=phase,
            model=agent_result.model,
            tokens_in=agent_result.tokens_in,
            tokens_out=agent_result.tokens_out,
            tokens_cached=agent_result.tokens_cached,
            cost_usd=agent_result.cost_usd,
            duration_ms=agent_result.duration_ms,
            num_turns=agent_result.num_turns,
        ))
        self._emit("phase_end", {
            "issue": current.issue_number,
            "phase": phase.value,
            "model": agent_result.model,
            "tokens_in": agent_result.tokens_in,
            "tokens_out": agent_result.tokens_out,
            "cost_usd": round(agent_result.cost_usd, 6),
            "duration_ms": agent_result.duration_ms,
        })

    def record_verify(self, results: list[VerifyResult]) -> None:
        current = self._get_current()
        if current is None:
            return
        current.verify_stages = [
            VerifyStageMetrics(
                stage=v.stage, passed=v.passed,
                exit_code=v.exit_code, duration_ms=v.duration_ms,
            )
            for v in results
        ]
        issue_num = current.issue_number
        for v in results:
            self._emit("verify", {
                "issue": issue_num,
                "stage": v.stage,
                "passed": v.passed,
                "duration_ms": v.duration_ms,
            })

    def record_review(self, review: ReviewResult) -> None:
        current = self._get_current()
        if current is None:
            return
        current.review_findings = [
            {"severity": f.severity, "file": f.file, "description": f.description}
            for f in review.findings
        ]
        issue_num = current.issue_number
        for f in review.findings:
            self._emit("review", {
                "issue": issue_num,
                "severity": f.severity,
                "file": f.file,
                "description": f.description[:200],
            })

    def record_testplan(self, result: TestPlanResult) -> None:
        current = self._get_current()
        if current is None:
            return
        current.testplan_items = [
            {"criterion": i.criterion, "status": i.status, "evidence": i.evidence}
            for i in result.items
        ]

    def record_failure(self, category: FailureCategory) -> None:
        current = self._get_current()
        if current is None:
            return
        current.failure_category = category
        self._emit("failure", {
            "issue": current.issue_number,
            "category": category.value,
        })

    def end_issue(self, outcome: str = "") -> Optional[IssueTelemetry]:
        current = self._get_current()
        if current is None:
            return None
        current.outcome = outcome
        with self._lock:
            self._completed.append(current)
        self._set_current(None)
        self._emit("issue_end", {
            "issue": current.issue_number,
            "outcome": outcome,
            "cost_usd": round(current.total_cost_usd, 6),
            "tokens_in": current.total_tokens_in,
            "tokens_out": current.total_tokens_out,
        })
        return current

    @property
    def completed(self) -> list[IssueTelemetry]:
        with self._lock:
            return list(self._completed)

    @staticmethod
    def to_jsonl_dict(it: IssueTelemetry) -> dict:
        return {
            "phase_metrics": [
                {
                    "phase": p.phase.value,
                    "model": p.model,
                    "tokens_in": p.tokens_in,
                    "tokens_out": p.tokens_out,
                    "tokens_cached": p.tokens_cached,
                    "cost_usd": round(p.cost_usd, 6),
                    "duration_ms": p.duration_ms,
                    "num_turns": p.num_turns,
                    "cache_hit_rate": round(p.cache_hit_rate, 4),
                }
                for p in it.phases
            ],
            "verify_stages": [
                {
                    "stage": v.stage,
                    "passed": v.passed,
                    "exit_code": v.exit_code,
                    "duration_ms": v.duration_ms,
                }
                for v in it.verify_stages
            ],
            "review_findings": it.review_findings,
            "testplan_items": it.testplan_items,
            "failure_category": it.failure_category.value if it.failure_category else None,
            "cache_hit_rate": round(it.cache_hit_rate, 4),
            "total_phase_cost_usd": round(it.total_cost_usd, 6),
        }

    def run_summary(
        self, daily_tokens_used: int = 0, daily_cap_tokens: int = 0,
    ) -> RunSummary:
        with self._lock:
            completed = list(self._completed)
        outcomes = Counter(it.outcome for it in completed)
        phase_cost: dict[str, float] = {}
        phase_tokens: dict[str, int] = {}
        model_cost: dict[str, float] = {}
        total_in = 0
        total_out = 0
        total_cached = 0
        phase_detail: dict[str, list[int]] = {}
        model_tokens: dict[str, list[int]] = {}
        issue_agg: dict[int, list] = {}

        for it in completed:
            key = it.issue_number
            if key not in issue_agg:
                issue_agg[key] = [0, 0, 0, 0.0]
            for p in it.phases:
                phase_cost[p.phase.value] = phase_cost.get(p.phase.value, 0) + p.cost_usd
                phase_tokens[p.phase.value] = phase_tokens.get(p.phase.value, 0) + p.tokens_in + p.tokens_out
                model_cost[p.model] = model_cost.get(p.model, 0) + p.cost_usd
                total_in += p.tokens_in
                total_out += p.tokens_out
                total_cached += p.tokens_cached

                pd = phase_detail.setdefault(p.phase.value, [0, 0, 0])
                pd[0] += p.tokens_in
                pd[1] += p.tokens_out
                pd[2] += p.tokens_cached

                mt = model_tokens.setdefault(p.model, [0, 0, 0])
                mt[0] += p.tokens_in
                mt[1] += p.tokens_out
                mt[2] += p.tokens_cached

                ia = issue_agg[key]
                ia[0] += p.tokens_in
                ia[1] += p.tokens_out
                ia[2] += p.tokens_cached
                ia[3] += p.cost_usd

        failures = Counter(
            it.failure_category.value
            for it in completed
            if it.failure_category is not None
        )

        # Deduplicate issues: count unique issue numbers for issues_processed
        seen_issues = {it.issue_number for it in completed}

        return RunSummary(
            issues_processed=len(seen_issues),
            success_count=outcomes.get("success", 0),
            retry_count=outcomes.get("retry", 0),
            skip_count=outcomes.get("skip", 0),
            total_cost_usd=sum(phase_cost.values()),
            phase_cost_breakdown={k: round(v, 6) for k, v in phase_cost.items()},
            phase_token_breakdown=phase_tokens,
            per_model_cost={k: round(v, 6) for k, v in model_cost.items()},
            overall_cache_hit_rate=total_cached / total_in if total_in > 0 else 0.0,
            top_failure_reasons=failures.most_common(),
            total_tokens_in=total_in,
            total_tokens_out=total_out,
            total_tokens_cached=total_cached,
            daily_tokens_used=daily_tokens_used,
            daily_cap_tokens=daily_cap_tokens,
            phase_token_detail={k: tuple(v) for k, v in phase_detail.items()},
            per_model_tokens={k: tuple(v) for k, v in model_tokens.items()},
            per_issue_summary={k: tuple(v) for k, v in issue_agg.items()},
        )
