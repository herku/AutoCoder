from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from autocoder.types import AgentResult, Issue, Outcome, VerifyResult


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

    def log_dry_run(self, issues: list[Issue], reasons: Optional[dict[int, str]] = None) -> None:
        print(f"\n[DRY RUN] Would process {len(issues)} issues:")
        for i, issue in enumerate(issues, 1):
            line = f"  {i}. #{issue.number} [{issue.priority.value}] {issue.title}"
            if reasons and issue.number in reasons:
                line += f"\n     → {reasons[issue.number]}"
            print(line)
        print()

    def log_prioritization(self, issues: list[Issue], reasons: dict[int, str]) -> None:
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
                }
                for iss in issues
            ],
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

    def write_summary(self) -> None:
        successes = [a for a in self._attempts if a["outcome"] == "success"]
        retries = [a for a in self._attempts if a["outcome"] == "retry"]
        skips = [a for a in self._attempts if a["outcome"] == "skip"]
        total_cost = sum(a["cost_usd"] for a in self._attempts)
        total_tokens = sum(a["tokens_in"] + a["tokens_out"] for a in self._attempts)

        print(f"\n{'=' * 50}")
        print(f"AutoCoder Run Summary ({self._run_id})")
        print(f"{'=' * 50}")
        print(f"  PRs created:  {len(successes)}")
        print(f"  Retries:      {len(retries)}")
        print(f"  Skipped:      {len(skips)}")
        print(f"  Total cost:   ${total_cost:.4f}")
        print(f"  Total tokens: {total_tokens:,}")
        print(f"  Log file:     {self._log_path}")
        if skips:
            print(f"  Dead letter:  {self._dead_letter_path}")
        print(f"{'=' * 50}\n")

    def _append(self, path: Path, record: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
