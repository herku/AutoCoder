from __future__ import annotations

import subprocess
import sys

from autocoder.agent import build_prompt, build_plan_prompt, build_implement_prompt, invoke_agent
from autocoder.anticheat import audit_diff, protect_test_files, restore_test_files
from autocoder.budget import BudgetTracker
from autocoder.git import GitOps
from autocoder.issues import analyze_and_prioritize, fetch_issues
from autocoder.logger import RunLogger
from autocoder.pr import comment_failure, create_pr, label_failed, mark_ready, merge_pr
from autocoder.review import build_fix_prompt, review_pr_diff
from autocoder.sandbox import SandboxConfig, build_sandbox, build_plan_sandbox
from autocoder.testplan import (
    build_test_plan_fix_prompt,
    extract_acceptance_criteria,
    verify_test_plan,
)
from autocoder.types import (
    AgentError,
    AntiCheatViolation,
    Outcome,
    RateLimitError,
    RunConfig,
    TestPlanResult,
    VerificationError,
    commit_prefix,
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
        dependencies: dict[int, list[int]] = {}
        if cfg.auto_prioritize:
            print(f"Analyzing {len(issues)} issues for AI auto-prioritization...")
            issues, reasons, dependencies = analyze_and_prioritize(issues, cfg.repo_path, cfg.triage_model)
            log.log_prioritization(issues, reasons, dependencies)
            parts = []
            for i in issues:
                label = f"#{i.number}({i.priority.value})"
                blockers = dependencies.get(i.number, [])
                if blockers:
                    label += f" after {','.join(f'#{b}' for b in blockers)}"
                parts.append(label)
            print(f"Priority order: {', '.join(parts)}\n")

        if cfg.dry_run:
            log.log_dry_run(issues, reasons=reasons if reasons else None, dependencies=dependencies if dependencies else None)
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
            try:
                _process_issue(issue, cfg, git, budget, log)
            except RateLimitError as e:
                log.log_event("rate_limited", error=str(e))
                print(f"  Rate limit hit: {str(e)[:200]}")
                print("  Stopping — retrying won't help until limit resets.")
                break

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
    plan_sandbox = build_plan_sandbox(cfg) if cfg.plan_mode else None
    budget.reset_issue()

    for attempt in range(1, cfg.max_retries + 1):
        agent_result = None
        verify_results = []
        protected_files: list[str] = []

        checkpoint = git.save_checkpoint()
        branch = git.create_branch(issue.number, issue.title)

        try:
            # Protect test files if enabled
            if cfg.protect_tests:
                protected_files = protect_test_files(cfg.repo_path, cfg.test_patterns)

            # Stage 3: Agent execution
            print(f"  Attempt {attempt}/{cfg.max_retries}: Running agent...")

            if cfg.plan_mode and plan_sandbox:
                # Phase 1: Plan (read-only)
                plan_prompt = build_plan_prompt(issue)
                max_budget = budget.remaining_for_issue_usd(cfg.model)
                plan_result = invoke_agent(
                    plan_prompt, cfg.repo_path, cfg.model, cfg.effort, max_budget, plan_sandbox
                )
                budget.record(plan_result)
                if plan_result.is_error:
                    raise AgentError(plan_result.result_text)
                plan_text = plan_result.result_text

                # Phase 2: Implement with plan context
                prompt = build_implement_prompt(issue, plan_text, error_context)
            else:
                prompt = build_prompt(
                    issue,
                    error_context=error_context,
                    repo_path=cfg.repo_path,
                )

            max_budget = budget.remaining_for_issue_usd(cfg.model)
            agent_result = invoke_agent(prompt, cfg.repo_path, cfg.model, cfg.effort, max_budget, sandbox)
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

            # Stage 4.5: Test plan verification
            test_plan = TestPlanResult(items=[], raw_response="", all_passed=True)
            criteria = extract_acceptance_criteria(issue.body)
            if criteria:
                print(f"  Verifying test plan ({len(criteria)} criteria)...")
                test_plan = verify_test_plan(issue, git.diff_full(), cfg.repo_path, cfg.model)

                if not test_plan.all_passed:
                    failed_items = [i for i in test_plan.items if i.status == "fail"]
                    print(f"  Test plan: {len(failed_items)} criteria not met. Fixing...")
                    for item in failed_items:
                        print(f"    - {item.criterion[:80]}")

                    fix_prompt = build_test_plan_fix_prompt(issue, failed_items)
                    max_budget = budget.remaining_for_issue_usd(cfg.model)
                    fix_result = invoke_agent(
                        fix_prompt, cfg.repo_path, cfg.model, cfg.effort, max_budget, sandbox
                    )
                    budget.record(fix_result)

                    if not fix_result.is_error:
                        print(f"  Re-verifying after test plan fix...")
                        verify_results = run_verification(cfg)
                        if not all(v.passed for v in verify_results):
                            failed = next(v for v in verify_results if not v.passed)
                            error_context = format_failure(failed)
                            raise VerificationError(failed.stage, error_context)
                        test_plan = verify_test_plan(issue, git.diff_full(), cfg.repo_path, cfg.model)
                else:
                    print(f"  Test plan: all criteria met.")

            # Stage 5: Commit and PR
            diff_stats = git.diff_stats()
            git.commit_all(f"{commit_prefix(issue)}: resolve #{issue.number} — {issue.title}")
            main_branch = git.get_main_branch()
            summary = agent_result.result_text[:500] if agent_result else ""
            pr_url = create_pr(
                cfg.repo_path, issue, branch, base=main_branch,
                summary=summary,
                diff_stats=diff_stats,
                test_plan_items=test_plan.items or None,
                verify_results=verify_results,
            )

            # Stage 6: Post-PR review and merge (if enabled)
            if cfg.auto_merge:
                print(f"  Draft PR created: {pr_url}")
                merge_status = _post_pr_review_and_merge(
                    cfg, git, budget, issue, branch, pr_url, sandbox,
                )
                print(f"  {merge_status}\n")
            else:
                print(f"  PR created: {pr_url}\n")

            log.log_attempt(
                issue, attempt, agent_result, verify_results,
                Outcome.SUCCESS, pr_url=pr_url, diff_stats=diff_stats,
            )
            return

        except RateLimitError:
            if protected_files:
                restore_test_files(protected_files)
            git.rollback(checkpoint)
            git.checkout_main()
            raise  # Propagate to run() to stop all processing

        except (AgentError, VerificationError, AntiCheatViolation, RuntimeError) as e:
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


def _post_pr_review_and_merge(
    cfg: RunConfig,
    git: GitOps,
    budget: BudgetTracker,
    issue,
    branch: str,
    pr_url: str,
    sandbox: SandboxConfig,
) -> str:
    """Review PR, fix issues, and squash-merge. Returns status string."""
    try:
        pre_fix_sha = git.save_checkpoint()

        # Review the diff
        print("  Running code review...")
        diff = git.diff_full()
        review = review_pr_diff(diff, cfg.repo_path, cfg.model)

        if not review.has_actionable_issues:
            print("  Review: no critical/medium issues found.")
            return _do_merge(cfg.repo_path, pr_url, "Merged (clean)")

        # Log findings
        for f in review.findings:
            print(f"  Review [{f.severity.upper()}] {f.file}: {f.description}")

        # Fix issues
        print(f"  Fixing {len(review.findings)} review issue(s)...")
        fix_prompt = build_fix_prompt(review.findings)
        max_budget = budget.remaining_for_issue_usd(cfg.model)
        try:
            fix_result = invoke_agent(fix_prompt, cfg.repo_path, cfg.model, cfg.effort, max_budget, sandbox)
            budget.record(fix_result)
        except RateLimitError:
            raise
        except AgentError as e:
            print(f"  Fix agent failed: {str(e)[:100]}")
            return _do_merge(cfg.repo_path, pr_url, "Merged (fix agent failed, using original)")

        if fix_result.is_error:
            return _do_merge(cfg.repo_path, pr_url, "Merged (fix agent error, using original)")

        # Re-verify after fixes
        print("  Re-verifying after review fixes...")
        verify_results = run_verification(cfg)
        if not all(v.passed for v in verify_results):
            print("  Review fixes broke tests, reverting to original.")
            git.rollback(pre_fix_sha)
            git.push_branch(branch)
            return _do_merge(cfg.repo_path, pr_url, "Merged (fix broke tests, reverted)")

        # Push review fixes
        git.commit_all(f"{commit_prefix(issue)}: address review feedback for #{issue.number}")
        git.push_branch(branch)
        return _do_merge(cfg.repo_path, pr_url, "Merged (with review fixes)")

    except RateLimitError:
        raise  # Propagate to stop all processing
    except Exception as e:
        print(f"  Review/merge error: {str(e)[:200]}")
        return _do_merge(cfg.repo_path, pr_url, "Merged (review failed, using original)")


def _do_merge(repo_path: str, pr_url: str, status: str) -> str:
    """Mark PR ready and squash-merge. Returns status string."""
    mark_ready(repo_path, pr_url)
    if merge_pr(repo_path, pr_url):
        return status
    return f"PR ready but merge failed (may need approval): {pr_url}"
