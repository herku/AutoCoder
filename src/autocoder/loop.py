from __future__ import annotations

import subprocess
import sys

from autocoder.agent import build_prompt, invoke_agent
from autocoder.anticheat import audit_diff, protect_test_files, restore_test_files
from autocoder.budget import BudgetTracker
from autocoder.git import GitOps
from autocoder.issues import analyze_and_prioritize, fetch_issues
from autocoder.logger import RunLogger
from autocoder.pr import comment_failure, create_pr, label_failed
from autocoder.sandbox import build_sandbox
from autocoder.types import (
    AgentError,
    AntiCheatViolation,
    Outcome,
    RunConfig,
    VerificationError,
)
from autocoder.verify import format_failure, run_verification


def run(cfg: RunConfig) -> None:
    log = RunLogger(cfg.log_dir)
    budget = BudgetTracker(cfg.token_budget, cfg.daily_cap)
    git = GitOps(cfg.repo_path)

    # Startup — check clean BEFORE creating lockfile
    git.assert_clean()
    git.acquire_lock()
    try:
        git.cleanup_orphan_branches()

        # Stage 1: Fetch all issues (no limit)
        if cfg.labels:
            print(f"Fetching issues with labels: {', '.join(cfg.labels)}...")
        else:
            print("Fetching all open issues...")
        issues = fetch_issues(cfg.repo_path, cfg.labels, limit=cfg.max_analyze)

        if not issues:
            print("No issues found. Exiting.")
            return

        print(f"Found {len(issues)} issues.")

        # Stage 1.5: Auto-prioritize (analyzes ALL issues)
        reasons: dict[int, str] = {}
        if cfg.auto_prioritize:
            print(f"Analyzing {len(issues)} issues for AI auto-prioritization...")
            issues, reasons = analyze_and_prioritize(issues, cfg.repo_path, cfg.triage_model)
            log.log_prioritization(issues, reasons)
            print(f"Priority order: {', '.join(f'#{i.number}({i.priority.value})' for i in issues)}\n")

        if cfg.dry_run:
            log.log_dry_run(issues, reasons=reasons if reasons else None)
            return

        # Truncate to max_issues for processing
        issues = issues[:cfg.max_issues]
        print(f"Processing top {len(issues)} issues.\n")

        for i, issue in enumerate(issues, 1):
            if budget.daily_exhausted():
                log.log_event("daily_cap_reached", **budget.summary())
                print("Daily token cap reached. Stopping.")
                break

            print(f"[{i}/{len(issues)}] Processing #{issue.number}: {issue.title}")
            _process_issue(issue, cfg, git, budget, log)

        log.write_summary()

    finally:
        git.release_lock()


def _process_issue(
    issue, cfg: RunConfig, git: GitOps, budget: BudgetTracker, log: RunLogger
) -> None:
    error_context = ""
    agent_result = None
    verify_results = []
    sandbox = build_sandbox(cfg)
    budget.reset_issue()

    for attempt in range(1, cfg.max_retries + 1):
        agent_result = None
        verify_results = []
        protected_files: list[str] = []

        checkpoint = git.save_checkpoint()
        branch = git.create_branch(issue.number)

        try:
            # Protect test files if enabled
            if cfg.protect_tests:
                protected_files = protect_test_files(cfg.repo_path, cfg.test_patterns)

            # Stage 3: Agent execution
            print(f"  Attempt {attempt}/{cfg.max_retries}: Running agent...")
            prompt = build_prompt(
                issue,
                error_context=error_context,
                repo_path=cfg.repo_path,
            )
            max_budget = budget.remaining_for_issue_usd(cfg.model)
            agent_result = invoke_agent(prompt, cfg.repo_path, cfg.model, max_budget, sandbox)
            budget.record(agent_result)

            if agent_result.is_error:
                raise AgentError(agent_result.result_text)

            # Restore test files before verification
            if protected_files:
                restore_test_files(protected_files)
                protected_files = []

            # Anti-cheat audit
            if cfg.protect_tests:
                audit_diff(cfg.repo_path, cfg.test_patterns)

            # Stage 4: Verification
            print(f"  Running verification...")
            verify_results = run_verification(cfg)

            if not all(v.passed for v in verify_results):
                failed = next(v for v in verify_results if not v.passed)
                error_context = format_failure(failed)
                raise VerificationError(failed.stage, error_context)

            # Stage 5: Commit and PR
            diff_stats = git.diff_stats()
            git.commit_all(f"fix: resolve #{issue.number} — {issue.title}")
            pr_url = create_pr(cfg.repo_path, issue, branch)

            log.log_attempt(
                issue, attempt, agent_result, verify_results,
                Outcome.SUCCESS, pr_url=pr_url, diff_stats=diff_stats,
            )
            print(f"  PR created: {pr_url}\n")
            return

        except (AgentError, VerificationError, AntiCheatViolation) as e:
            # Restore test files on failure
            if protected_files:
                restore_test_files(protected_files)

            git.rollback(checkpoint)
            git.checkout_main()

            if attempt < cfg.max_retries:
                log.log_attempt(
                    issue, attempt, agent_result, verify_results,
                    Outcome.RETRY, error=str(e),
                )
                print(f"  Attempt {attempt} failed: {str(e)[:200]}")
                if not error_context:
                    error_context = str(e)
            else:
                # Final failure
                log.log_attempt(
                    issue, attempt, agent_result, verify_results,
                    Outcome.SKIP, error=str(e),
                )
                log.dead_letter(issue, str(e))
                label_failed(cfg.repo_path, issue.number)
                comment_failure(cfg.repo_path, issue.number, str(e))
                git.delete_branch(branch)
                print(f"  Skipped after {cfg.max_retries} attempts.\n")
                return

        except subprocess.TimeoutExpired:
            if protected_files:
                restore_test_files(protected_files)
            git.rollback(checkpoint)
            git.checkout_main()
            error_context = "Previous attempt timed out. Use a simpler approach."
            log.log_attempt(
                issue, attempt, agent_result, verify_results,
                Outcome.RETRY, error="timeout",
            )
            print(f"  Attempt {attempt} timed out.")

    # Should not reach here, but safety net
    git.checkout_main()
