from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from autocoder.types import AgentResult, Issue, Outcome, VerifyResult

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autocoder.telemetry import IssueTelemetry, Telemetry


class RunLogger:
    def __init__(self, log_dir: str):
        self._run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._log_dir = Path(log_dir)
        self._log_path = self._log_dir / f"run_{self._run_id}.jsonl"
        self._dead_letter_path = self._log_dir / "failed_issues.jsonl"
        self._attempts: list[dict] = []

    def log_attempt(
        self,
        issue: Issue,
        attempt: int,
        agent_result: Optional[AgentResult],
        verify_results: list[VerifyResult],
        outcome: Outcome,
        pr_url: Optional[str] = None,
        diff_stats: str = "",
        error: Optional[str] = None,
        telemetry: Optional[IssueTelemetry] = None,
    ) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self._run_id,
            "issue_num": issue.number,
            "issue_title": issue.title,
            "attempt": attempt,
            "model": agent_result.model if agent_result else None,
            "tokens_in": agent_result.tokens_in if agent_result else 0,
            "tokens_out": agent_result.tokens_out if agent_result else 0,
            "tokens_cached": agent_result.tokens_cached if agent_result else 0,
            "duration_ms": agent_result.duration_ms if agent_result else 0,
            "cost_usd": agent_result.cost_usd if agent_result else 0,
            "num_turns": agent_result.num_turns if agent_result else 0,
            "test_results": [
                {"stage": v.stage, "passed": v.passed, "exit_code": v.exit_code}
                for v in verify_results
            ],
            "git_diff_stats": diff_stats,
            "outcome": outcome.value,
            "pr_url": pr_url,
            "error": error,
        }
        if telemetry is not None:
            from autocoder.telemetry import Telemetry
            record.update(Telemetry.to_jsonl_dict(telemetry))
        self._append(self._log_path, record)
        self._attempts.append(record)

    def log_event(self, event: str, **extra: object) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self._run_id,
            "event": event,
            **extra,
        }
        self._append(self._log_path, record)

    def log_dry_run(
        self,
        issues: list[Issue],
        reasons: Optional[dict[int, str]] = None,
        dependencies: Optional[dict[int, list[int]]] = None,
    ) -> None:
        print(f"\n[DRY RUN] Would process {len(issues)} issues:")
        for i, issue in enumerate(issues, 1):
            line = f"  {i}. #{issue.number} [{issue.priority.value}] {issue.title}"
            if reasons and issue.number in reasons:
                line += f"\n     → {reasons[issue.number]}"
            if dependencies and issue.number in dependencies and dependencies[issue.number]:
                blockers = ", ".join(f"#{b}" for b in dependencies[issue.number])
                line += f"\n     Blocked by: {blockers}"
            print(line)
        print()

    def log_prioritization(
        self,
        issues: list[Issue],
        reasons: dict[int, str],
        dependencies: dict[int, list[int]] | None = None,
    ) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self._run_id,
            "event": "auto_prioritize",
            "results": [
                {
                    "issue_num": iss.number,
                    "title": iss.title,
                    "priority": iss.priority.value,
                    "reason": reasons.get(iss.number, ""),
                    "blocked_by": (dependencies or {}).get(iss.number, []),
                }
                for iss in issues
            ],
        }
        self._append(self._log_path, record)

    def log_timings(self, steps: list) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self._run_id,
            "event": "step_timings",
            "steps": [{"name": s.name, "duration_ms": s.duration_ms} for s in steps],
            "total_ms": sum(s.duration_ms for s in steps),
        }
        self._append(self._log_path, record)

    def dead_letter(self, issue: Issue, error: str) -> None:
        self._append(
            self._dead_letter_path,
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "run_id": self._run_id,
                "issue_num": issue.number,
                "title": issue.title,
                "url": issue.url,
                "error": error,
            },
        )

    def log_run_summary(self, telem: Telemetry) -> None:
        from autocoder.telemetry import Telemetry as _T  # noqa: runtime import
        summary = telem.run_summary()
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self._run_id,
            "event": "run_summary",
            "issues_processed": summary.issues_processed,
            "success_count": summary.success_count,
            "retry_count": summary.retry_count,
            "skip_count": summary.skip_count,
            "total_cost_usd": round(summary.total_cost_usd, 6),
            "phase_cost_breakdown": summary.phase_cost_breakdown,
            "phase_token_breakdown": summary.phase_token_breakdown,
            "per_model_cost": summary.per_model_cost,
            "overall_cache_hit_rate": round(summary.overall_cache_hit_rate, 4),
            "top_failure_reasons": summary.top_failure_reasons,
        }
        self._append(self._log_path, record)

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def log_path(self) -> Path:
        return self._log_path

    @property
    def dead_letter_path(self) -> Path:
        return self._dead_letter_path

    def _append(self, path: Path, record: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
