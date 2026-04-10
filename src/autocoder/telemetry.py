from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from autocoder.types import AgentResult, ReviewResult, TestPlanResult, VerifyResult


class Phase(str, Enum):
    PLAN = "plan"
    IMPLEMENT = "implement"
    REVIEW_FIX = "review_fix"
    TESTPLAN_FIX = "testplan_fix"
    UPDATE_CLAUDE_MD = "update_claude_md"


class FailureCategory(str, Enum):
    LINT_FAIL = "lint_fail"
    TEST_FAIL = "test_fail"
    INTEGRATION_FAIL = "integration_fail"
    REVIEW_REJECTED = "review_rejected"
    TESTPLAN_FAIL = "testplan_fail"
    RATE_LIMIT = "rate_limit"
    AUTH_ERROR = "auth_error"
    BUDGET_EXHAUSTED = "budget_exhausted"
    AGENT_ERROR = "agent_error"
    ANTICHEAT_VIOLATION = "anticheat_violation"
    TIMEOUT = "timeout"


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


class Telemetry:
    """Central telemetry collector for a single AutoCoder run."""

    def __init__(self) -> None:
        self._current: Optional[IssueTelemetry] = None
        self._completed: list[IssueTelemetry] = []

    def begin_issue(self, issue_number: int, attempt: int) -> None:
        # If a previous issue wasn't ended (e.g. unexpected exception), end it now
        if self._current is not None:
            self._completed.append(self._current)
        self._current = IssueTelemetry(issue_number=issue_number, attempt=attempt)

    def record_phase(self, phase: Phase, agent_result: AgentResult) -> None:
        if self._current is None:
            return
        self._current.phases.append(PhaseMetrics(
            phase=phase,
            model=agent_result.model,
            tokens_in=agent_result.tokens_in,
            tokens_out=agent_result.tokens_out,
            tokens_cached=agent_result.tokens_cached,
            cost_usd=agent_result.cost_usd,
            duration_ms=agent_result.duration_ms,
            num_turns=agent_result.num_turns,
        ))

    def record_verify(self, results: list[VerifyResult]) -> None:
        if self._current is None:
            return
        self._current.verify_stages = [
            VerifyStageMetrics(
                stage=v.stage, passed=v.passed,
                exit_code=v.exit_code, duration_ms=v.duration_ms,
            )
            for v in results
        ]

    def record_review(self, review: ReviewResult) -> None:
        if self._current is None:
            return
        self._current.review_findings = [
            {"severity": f.severity, "file": f.file, "description": f.description}
            for f in review.findings
        ]

    def record_testplan(self, result: TestPlanResult) -> None:
        if self._current is None:
            return
        self._current.testplan_items = [
            {"criterion": i.criterion, "status": i.status, "evidence": i.evidence}
            for i in result.items
        ]

    def record_failure(self, category: FailureCategory) -> None:
        if self._current is None:
            return
        self._current.failure_category = category

    def end_issue(self, outcome: str = "") -> Optional[IssueTelemetry]:
        if self._current is None:
            return None
        self._current.outcome = outcome
        completed = self._current
        self._completed.append(completed)
        self._current = None
        return completed

    @property
    def completed(self) -> list[IssueTelemetry]:
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

    def run_summary(self) -> RunSummary:
        outcomes = Counter(it.outcome for it in self._completed)
        phase_cost: dict[str, float] = {}
        phase_tokens: dict[str, int] = {}
        model_cost: dict[str, float] = {}
        total_in = 0
        total_cached = 0

        for it in self._completed:
            for p in it.phases:
                phase_cost[p.phase.value] = phase_cost.get(p.phase.value, 0) + p.cost_usd
                phase_tokens[p.phase.value] = phase_tokens.get(p.phase.value, 0) + p.tokens_in + p.tokens_out
                model_cost[p.model] = model_cost.get(p.model, 0) + p.cost_usd
                total_in += p.tokens_in
                total_cached += p.tokens_cached

        failures = Counter(
            it.failure_category.value
            for it in self._completed
            if it.failure_category is not None
        )

        # Deduplicate issues: count unique issue numbers for issues_processed
        seen_issues = {it.issue_number for it in self._completed}

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
        )
