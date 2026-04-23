"""Tests for git worktree helpers and parallel thread-safety."""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from autocoder.budget import BudgetTracker
from autocoder.git import GitOps
from autocoder.telemetry import FailureCategory, Phase, Telemetry
from autocoder.types import AgentResult


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "test"], check=True,
    )
    # Seed a commit so main has history
    (root / "README.md").write_text("seed\n")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "seed"], check=True,
    )


def test_create_and_remove_worktree(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    git = GitOps(str(repo))

    wt = tmp_path / "wt-1"
    git.create_worktree(str(wt), "feat-test-1", base="main")
    assert wt.exists()
    assert (wt / "README.md").exists()

    # Worktree branch should exist
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "feat-test-1"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "feat-test-1" in branches

    git.remove_worktree(str(wt))
    assert not wt.exists()


def test_create_worktree_force_replaces_existing_branch(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    git = GitOps(str(repo))

    subprocess.run(
        ["git", "-C", str(repo), "branch", "feat-clash"], check=True,
    )
    wt = tmp_path / "wt-2"
    # Should succeed even though branch pre-exists (force delete)
    git.create_worktree(str(wt), "feat-clash", base="main")
    assert wt.exists()
    git.remove_worktree(str(wt))


def test_prune_worktrees_tolerates_no_records(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    GitOps(str(repo)).prune_worktrees()  # must not raise


# ---- BudgetTracker thread-safety ----


def _result(tokens: int = 100, cost: float = 0.01) -> AgentResult:
    return AgentResult(
        session_id="s", result_text="", is_error=False, duration_ms=1,
        tokens_in=tokens // 2, tokens_out=tokens - tokens // 2,
        tokens_cached=0, cost_usd=cost, num_turns=1, model="sonnet",
    )


def test_budget_per_issue_tokens_are_thread_local():
    tracker = BudgetTracker(per_issue_token_budget=1000, daily_cap_tokens=100_000)
    barrier = threading.Barrier(2)
    seen: dict[str, float] = {}

    def worker(name: str):
        tracker.reset_issue()
        tracker.record(_result(tokens=200))
        barrier.wait()
        # Other thread also recorded 200, but our per-issue view is isolated
        seen[name] = tracker.remaining_for_issue_usd("sonnet")

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    # Both threads saw only 200 tokens consumed locally
    assert seen["a"] == seen["b"]
    # Daily totals aggregate across threads
    assert tracker.daily_tokens_used == 400


def test_budget_daily_cap_respected_across_threads():
    tracker = BudgetTracker(per_issue_token_budget=10_000, daily_cap_tokens=1000)
    def worker():
        tracker.reset_issue()
        for _ in range(20):
            tracker.record(_result(tokens=50))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()

    # 4 workers * 20 iterations * 50 tokens = 4000 total
    assert tracker.daily_tokens_used == 4000
    assert tracker.daily_exhausted()


# ---- Telemetry thread-safety ----


def test_telemetry_current_issue_is_thread_local():
    telem = Telemetry()
    from autocoder.types import AgentResult

    errors: list[str] = []

    def worker(issue_num: int):
        try:
            telem.begin_issue(issue_num, attempt=1, title=f"issue {issue_num}")
            # Each worker records phases; must not leak into other thread's current
            telem.record_phase(Phase.IMPLEMENT, _result(tokens=100, cost=0.05))
            it = telem.end_issue(outcome="success")
            assert it is not None
            assert it.issue_number == issue_num
            assert len(it.phases) == 1
        except AssertionError as e:
            errors.append(f"{issue_num}: {e}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(1, 6)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert not errors, errors

    completed = telem.completed
    assert len(completed) == 5
    assert {it.issue_number for it in completed} == {1, 2, 3, 4, 5}
    # Each should have exactly one phase
    assert all(len(it.phases) == 1 for it in completed)
