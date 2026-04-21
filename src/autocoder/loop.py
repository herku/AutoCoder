from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass

from pathlib import Path

from autocoder.agent import (
    build_prompt, build_plan_prompt, build_implement_prompt,
    build_update_claude_md_prompt, build_ci_learn_prompt, build_impl_learn_prompt,
    invoke_agent, TIMEOUT_PLAN, TIMEOUT_IMPLEMENT, TIMEOUT_BUILD_FIX,
    TIMEOUT_CLAUDE_MD, BUDGET_CLAUDE_MD, BUDGET_CI_LEARN, BUDGET_IMPL_LEARN,
)
from autocoder.anticheat import audit_diff, protect_test_files, restore_test_files
from autocoder.budget import BudgetTracker
from autocoder.git import GitOps
from autocoder.epic import process_epic
from autocoder.issues import analyze_and_prioritize, fetch_issues, fetch_issues_by_number, parse_sub_issues
from autocoder.logger import RunLogger
from autocoder.pr import comment_failure, create_pr, label_failed, mark_ready, merge_pr, wait_for_ci, wait_for_new_checks
from autocoder.review import build_build_fix_prompt, build_ci_fix_prompt, build_fix_prompt, review_pr_diff
from autocoder.sandbox import SandboxConfig, build_sandbox, build_plan_sandbox, build_claude_md_sandbox
from autocoder.testplan import (
    build_test_plan_fix_prompt,
    extract_acceptance_criteria,
    verify_test_plan,
)
from autocoder.telemetry import FailureCategory, Phase, Telemetry
from autocoder.types import (
    AgentError,
    AntiCheatViolation,
    AuthenticationError,
    Outcome,
    RateLimitError,
    RunConfig,
    TestPlanResult,
    VerificationError,
    commit_prefix,
    is_epic,
)
from autocoder.verify import _run_step, format_failure, run_verification


# ---------------------------------------------------------------------------
# Step timing
# ---------------------------------------------------------------------------

def _fmt_dur(ms: int) -> str:
    if ms >= 60_000:
        return f"{ms / 60_000:.1f}m"
    if ms >= 1000:
        return f"{ms / 1000:.1f}s"
    return f"{ms}ms"


@dataclass
class StepTiming:
    name: str
    duration_ms: int


class StepTimings:
    def __init__(self) -> None:
        self._steps: list[StepTiming] = []

    def record(self, name: str, duration_ms: int) -> None:
        self._steps.append(StepTiming(name, duration_ms))

    @property
    def steps(self) -> list[StepTiming]:
        return list(self._steps)

    @property
    def total_ms(self) -> int:
        return sum(s.duration_ms for s in self._steps)


class StepTimer:
    """Context manager that times a step, prints it, and records it."""

    def __init__(self, name: str, timings: StepTimings) -> None:
        self._name = name
        self._timings = timings
        self._start: float = 0

    def __enter__(self) -> "StepTimer":
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc: object) -> bool:
        duration_ms = int((time.monotonic() - self._start) * 1000)
        self._timings.record(self._name, duration_ms)
        print(f"  \u23f1 {self._name}: {_fmt_dur(duration_ms)}")
        return False


def _print_timing_summary(timings: StepTimings) -> None:
    steps = timings.steps
    if not steps:
        return
    max_name = max(len(s.name) for s in steps)
    w = max(max_name, 5) + 2
    print(f"\nStep Timings:")
    for s in steps:
        print(f"  {s.name:<{w}} {_fmt_dur(s.duration_ms):>10}")
    print(f"  {'─' * (w + 10)}")
    print(f"  {'TOTAL':<{w}} {_fmt_dur(timings.total_ms):>10}")


def run(cfg: RunConfig) -> None:
    log = RunLogger(cfg.log_dir)
    budget = BudgetTracker(cfg.token_budget, cfg.daily_cap)
    git = GitOps(cfg.repo_path)
    timings = StepTimings()
    telem = Telemetry()

    # Startup — check clean BEFORE creating lockfile
    git.assert_clean()
    git.acquire_lock()
    try:
        git.cleanup_orphan_branches()

        # Stage 1: Fetch issues
        with StepTimer("fetch_issues", timings):
            if cfg.issue_numbers:
                print(f"Fetching issues: {', '.join(f'#{n}' for n in cfg.issue_numbers)}...")
                issues = fetch_issues_by_number(cfg.repo_path, cfg.issue_numbers)
            elif cfg.labels:
                print(f"Fetching issues with labels: {', '.join(cfg.labels)}...")
                issues = fetch_issues(cfg.repo_path, cfg.labels, limit=cfg.max_analyze)
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
            with StepTimer("prioritize", timings):
                issues, reasons, dependencies = analyze_and_prioritize(
                    issues, cfg.repo_path, cfg.triage_model, force=cfg.force_prioritize,
                )
            log.log_prioritization(issues, reasons, dependencies)
            parts = []
            for i in issues:
                label = f"#{i.number}({i.priority.value})"
                blockers = dependencies.get(i.number, [])
                if blockers:
                    label += f" after {','.join(f'#{b}' for b in blockers)}"
                parts.append(label)
            print(f"Priority order: {', '.join(parts)}\n")

        # Partition epics vs regular issues
        epic_issues = [i for i in issues if is_epic(i)]
        regular_issues = [i for i in issues if not is_epic(i)]

        if cfg.dry_run:
            log.log_dry_run(issues, reasons=reasons if reasons else None, dependencies=dependencies if dependencies else None)
            for epic in epic_issues:
                sub_nums = parse_sub_issues(epic.body)
                print(f"  Epic #{epic.number}: {epic.title} ({len(sub_nums)} sub-issues: {', '.join(f'#{n}' for n in sub_nums)})")
            return

        # Pre-flight: verify build passes on main before processing any issues
        if cfg.build_cmd:
            print(f"Pre-flight build check: {cfg.build_cmd}")
            preflight = _run_step(cfg.build_cmd, cfg.repo_path, "build")
            if not preflight.passed:
                output = (preflight.stderr or preflight.stdout or "").strip()
                tail = "\n".join(output.split("\n")[-20:])
                print(f"  FAILED — main branch does not build.\n  {tail}")
                print("  Fix the build on main before running AutoCoder.")
                return

        # Truncate regular issues to max_issues for processing
        regular_issues = regular_issues[:cfg.max_issues]
        print(f"Processing {len(regular_issues)} regular issues + {len(epic_issues)} epics.\n")

        # Process regular issues first (may include epic sub-issues)
        for i, issue in enumerate(regular_issues, 1):
            if budget.daily_exhausted():
                log.log_event("daily_cap_reached", **budget.summary())
                print("Daily token cap reached. Stopping.")
                break

            print(f"[{i}/{len(regular_issues)}] Processing #{issue.number}: {issue.title}")
            try:
                process_issue(issue, cfg, git, budget, log, timings, telem)
            except RateLimitError as e:
                log.log_event("rate_limited", error=str(e))
                print(f"  Rate limit hit: {str(e)[:200]}")
                print("  Stopping — retrying won't help until limit resets.")
                break
            except AuthenticationError as e:
                log.log_event("auth_failed", error=str(e))
                print(f"  Authentication failed: {str(e)[:200]}")
                print("  Stopping — token expired or invalid. Re-authenticate and retry.")
                break

        # Process epics (sub-issues implemented, then epic closed)
        for i, epic in enumerate(epic_issues, 1):
            if budget.daily_exhausted():
                log.log_event("daily_cap_reached", **budget.summary())
                print("Daily token cap reached. Stopping.")
                break

            print(f"[Epic {i}/{len(epic_issues)}] Processing #{epic.number}: {epic.title}")
            try:
                result = process_epic(epic, cfg, git, budget, log, timings, telem)
                log.log_event(
                    "epic_processed", epic_number=epic.number,
                    succeeded=result.succeeded, failed=result.failed,
                    skipped_closed=result.skipped_closed, all_complete=result.all_complete,
                )
            except RateLimitError as e:
                log.log_event("rate_limited", error=str(e))
                print(f"  Rate limit hit: {str(e)[:200]}")
                print("  Stopping — retrying won't help until limit resets.")
                break
            except AuthenticationError as e:
                log.log_event("auth_failed", error=str(e))
                print(f"  Authentication failed: {str(e)[:200]}")
                print("  Stopping — token expired or invalid. Re-authenticate and retry.")
                break

        _print_timing_summary(timings)
        log.log_timings(timings.steps)
        log.log_run_summary(telem)
        _print_run_summary(telem, log, budget)

    finally:
        git.release_lock()


def process_issue(
    issue, cfg: RunConfig, git: GitOps, budget: BudgetTracker, log: RunLogger,
    timings: StepTimings, telem: Telemetry,
) -> None:
    error_context = ""
    agent_result = None
    verify_results = []
    build_failures = 0
    sandbox = build_sandbox(cfg)
    plan_sandbox = build_plan_sandbox(cfg) if cfg.plan_mode else None
    budget.reset_issue()
    tag = f"#{issue.number}"

    for attempt in range(1, cfg.max_retries + 1):
        telem.begin_issue(issue.number, attempt)
        agent_result = None
        verify_results = []
        protected_files: list[str] = []
        att = f"att{attempt}"

        checkpoint = git.save_checkpoint()
        with StepTimer(f"create_branch {tag}", timings):
            branch = git.create_branch(issue.number, issue.title)

        try:
            # Protect test files if enabled
            if cfg.protect_tests:
                protected_files = protect_test_files(cfg.repo_path, cfg.test_patterns)

            # Stage 3: Agent execution
            print(f"  Attempt {attempt}/{cfg.max_retries}: Running agent...")

            if cfg.plan_mode and plan_sandbox:
                # Phase 1: Plan (read-only)
                with StepTimer(f"plan {tag} {att}", timings):
                    plan_prompt = build_plan_prompt(issue)
                    max_budget = budget.remaining_for_issue_usd(cfg.plan_model)
                    plan_result = invoke_agent(
                        plan_prompt, cfg.repo_path, cfg.plan_model, cfg.effort, max_budget, plan_sandbox,
                        timeout=TIMEOUT_PLAN,
                    )
                budget.record(plan_result)
                telem.record_phase(Phase.PLAN, plan_result)
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

            with StepTimer(f"agent {tag} {att}", timings):
                max_budget = budget.remaining_for_issue_usd(cfg.model)
                agent_result = invoke_agent(prompt, cfg.repo_path, cfg.model, cfg.effort, max_budget, sandbox)
            budget.record(agent_result)
            telem.record_phase(Phase.IMPLEMENT, agent_result)

            if agent_result.is_error:
                raise AgentError(agent_result.result_text)

            # Restore test files before verification
            if protected_files:
                restore_test_files(protected_files)
                protected_files = []

            # Anti-cheat audit
            if cfg.protect_tests:
                with StepTimer(f"anticheat {tag}", timings):
                    audit_diff(cfg.repo_path, cfg.test_patterns)

            # Stage 4: Verification
            print(f"  Running verification...")
            with StepTimer(f"verify {tag} {att}", timings):
                verify_results = run_verification(cfg)
            telem.record_verify(verify_results)

            if not all(v.passed for v in verify_results):
                failed = next(v for v in verify_results if not v.passed)
                if failed.stage == "build":
                    # Stage 4a: Attempt focused build fix before giving up
                    print(f"  Build failed. Attempting fix...")
                    with StepTimer(f"build_fix {tag} {att}", timings):
                        fix_prompt = build_build_fix_prompt(format_failure(failed), cfg.build_cmd or "")
                        max_budget = budget.remaining_for_issue_usd(cfg.model)
                        fix_result = invoke_agent(
                            fix_prompt, cfg.repo_path, cfg.model, cfg.effort, max_budget, sandbox,
                            timeout=TIMEOUT_BUILD_FIX,
                        )
                    budget.record(fix_result)
                    telem.record_phase(Phase.BUILD_FIX, fix_result)

                    if not fix_result.is_error:
                        print(f"  Re-verifying after build fix...")
                        with StepTimer(f"re_verify {tag} {att}", timings):
                            verify_results = run_verification(cfg)
                        telem.record_verify(verify_results)

                    if fix_result.is_error or not all(v.passed for v in verify_results):
                        failed = next((v for v in verify_results if not v.passed), failed)
                        error_context = format_failure(failed)
                        raise VerificationError(failed.stage, error_context)
                else:
                    error_context = format_failure(failed)
                    raise VerificationError(failed.stage, error_context)

            # Stage 4.5: Test plan verification
            test_plan = TestPlanResult(items=[], raw_response="", all_passed=True)
            criteria = extract_acceptance_criteria(issue.body)
            if criteria:
                print(f"  Verifying test plan ({len(criteria)} criteria)...")
                with StepTimer(f"test_plan {tag}", timings):
                    test_plan = verify_test_plan(issue, git.diff_full(), cfg.repo_path, cfg.model)

                telem.record_testplan(test_plan)

                if not test_plan.all_passed:
                    failed_items = [i for i in test_plan.items if i.status == "fail"]
                    print(f"  Test plan: {len(failed_items)} criteria not met. Fixing...")
                    for item in failed_items:
                        print(f"    - {item.criterion[:80]}")

                    with StepTimer(f"test_plan_fix {tag}", timings):
                        fix_prompt = build_test_plan_fix_prompt(issue, failed_items)
                        max_budget = budget.remaining_for_issue_usd(cfg.model)
                        fix_result = invoke_agent(
                            fix_prompt, cfg.repo_path, cfg.model, cfg.effort, max_budget, sandbox
                        )
                    budget.record(fix_result)
                    telem.record_phase(Phase.TESTPLAN_FIX, fix_result)

                    if not fix_result.is_error:
                        print(f"  Re-verifying after test plan fix...")
                        with StepTimer(f"re_verify {tag}", timings):
                            verify_results = run_verification(cfg)
                        telem.record_verify(verify_results)
                        if not all(v.passed for v in verify_results):
                            failed = next(v for v in verify_results if not v.passed)
                            error_context = format_failure(failed)
                            raise VerificationError(failed.stage, error_context)
                        test_plan = verify_test_plan(issue, git.diff_full(), cfg.repo_path, cfg.model)
                        telem.record_testplan(test_plan)
                else:
                    print(f"  Test plan: all criteria met.")

            # Stage 4.9: Update CLAUDE.md
            if cfg.update_claude_md:
                try:
                    print(f"  Updating CLAUDE.md...")
                    with StepTimer(f"update_claude_md {tag}", timings):
                        claude_md_path = Path(cfg.repo_path) / "CLAUDE.md"
                        existing_content = claude_md_path.read_text() if claude_md_path.exists() else None
                        diff_for_md = git.diff_full()
                        md_prompt = build_update_claude_md_prompt(diff_for_md, existing_content)
                        md_sandbox = build_claude_md_sandbox(cfg)
                        md_budget = min(BUDGET_CLAUDE_MD, budget.remaining_for_issue_usd(cfg.model))
                        md_result = invoke_agent(
                            md_prompt, cfg.repo_path, cfg.model, cfg.effort,
                            md_budget, md_sandbox, timeout=TIMEOUT_CLAUDE_MD,
                        )
                    budget.record(md_result)
                    telem.record_phase(Phase.UPDATE_CLAUDE_MD, md_result)
                    if md_result.is_error:
                        print(f"  CLAUDE.md update failed (agent error), skipping.")
                    else:
                        print(f"  CLAUDE.md updated.")
                except Exception as e:
                    print(f"  CLAUDE.md update failed: {str(e)[:100]}, skipping.")

            # Stage 4.95: Learn from implementation
            try:
                print("  Capturing implementation learnings...")
                with StepTimer(f"impl_learn {tag}", timings):
                    impl_diff = git.diff_stats()
                    verify_summary = "\n".join(
                        f"- {v.stage}: {'PASS' if v.passed else 'FAIL'}" for v in verify_results
                    )
                    learn_prompt = build_impl_learn_prompt(impl_diff, verify_summary)
                    learn_sandbox = build_claude_md_sandbox(cfg)
                    learn_budget = min(BUDGET_IMPL_LEARN, budget.remaining_for_issue_usd(cfg.model))
                    learn_result = invoke_agent(
                        learn_prompt, cfg.repo_path, cfg.model, cfg.effort,
                        learn_budget, learn_sandbox, timeout=TIMEOUT_CLAUDE_MD,
                    )
                budget.record(learn_result)
                if learn_result.is_error:
                    print("  Implementation learnings failed (agent error), skipping.")
                else:
                    print("  Implementation learnings saved.")
            except Exception as e:
                print(f"  Implementation learnings failed: {str(e)[:100]}, skipping.")

            # Stage 5: Commit and PR
            with StepTimer(f"commit_pr {tag}", timings):
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
                with StepTimer(f"review_merge {tag}", timings):
                    merge_status = _post_pr_review_and_merge(
                        cfg, git, budget, issue, branch, pr_url, sandbox, telem,
                    )
                print(f"  {merge_status}\n")
            else:
                print(f"  PR created: {pr_url}\n")

            issue_telem = telem.end_issue(outcome=Outcome.SUCCESS.value)
            log.log_attempt(
                issue, attempt, agent_result, verify_results,
                Outcome.SUCCESS, pr_url=pr_url, diff_stats=diff_stats,
                telemetry=issue_telem,
            )
            return

        except RateLimitError:
            telem.record_failure(FailureCategory.RATE_LIMIT)
            telem.end_issue(outcome=Outcome.SKIP.value)
            if protected_files:
                restore_test_files(protected_files)
            git.rollback(checkpoint)
            git.checkout_main()
            raise  # Propagate to run() to stop all processing

        except (AgentError, VerificationError, AntiCheatViolation, RuntimeError) as e:
            # Classify failure for telemetry
            if isinstance(e, VerificationError):
                _stage_map = {
                    "build": FailureCategory.BUILD_FAIL,
                    "lint": FailureCategory.LINT_FAIL,
                    "unit": FailureCategory.TEST_FAIL,
                    "integration": FailureCategory.INTEGRATION_FAIL,
                }
                telem.record_failure(_stage_map.get(e.stage, FailureCategory.TEST_FAIL))

                # Separate build retry budget
                if e.stage == "build":
                    build_failures += 1
                    if build_failures > cfg.build_retries:
                        if protected_files:
                            restore_test_files(protected_files)
                        git.rollback(checkpoint)
                        git.checkout_main()
                        issue_telem = telem.end_issue(outcome=Outcome.SKIP.value)
                        log.log_attempt(
                            issue, attempt, agent_result, verify_results,
                            Outcome.SKIP, error=str(e), telemetry=issue_telem,
                        )
                        log.dead_letter(issue, f"Build failed after {cfg.build_retries} retries: {e}")
                        label_failed(cfg.repo_path, issue.number)
                        comment_failure(cfg.repo_path, issue.number, str(e))
                        git.delete_branch(branch)
                        print(f"  Build retries exhausted ({cfg.build_retries}). Dead-lettered.\n")
                        return
            elif isinstance(e, AntiCheatViolation):
                telem.record_failure(FailureCategory.ANTICHEAT_VIOLATION)
            elif isinstance(e, AgentError):
                telem.record_failure(FailureCategory.AGENT_ERROR)

            # Restore test files on failure
            if protected_files:
                restore_test_files(protected_files)

            git.rollback(checkpoint)
            git.checkout_main()

            if attempt < cfg.max_retries:
                issue_telem = telem.end_issue(outcome=Outcome.RETRY.value)
                log.log_attempt(
                    issue, attempt, agent_result, verify_results,
                    Outcome.RETRY, error=str(e), telemetry=issue_telem,
                )
                print(f"  Attempt {attempt} failed: {str(e)[:200]}")
                if not error_context:
                    error_context = str(e)
            else:
                # Final failure
                issue_telem = telem.end_issue(outcome=Outcome.SKIP.value)
                log.log_attempt(
                    issue, attempt, agent_result, verify_results,
                    Outcome.SKIP, error=str(e), telemetry=issue_telem,
                )
                log.dead_letter(issue, str(e))
                label_failed(cfg.repo_path, issue.number)
                comment_failure(cfg.repo_path, issue.number, str(e))
                git.delete_branch(branch)
                print(f"  Skipped after {cfg.max_retries} attempts.\n")
                return

        except subprocess.TimeoutExpired:
            telem.record_failure(FailureCategory.TIMEOUT)
            issue_telem = telem.end_issue(outcome=Outcome.RETRY.value)
            if protected_files:
                restore_test_files(protected_files)
            git.rollback(checkpoint)
            git.checkout_main()
            error_context = "Previous attempt timed out. Use a simpler approach."
            log.log_attempt(
                issue, attempt, agent_result, verify_results,
                Outcome.RETRY, error="timeout", telemetry=issue_telem,
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
    telem: Telemetry,
) -> str:
    """Review PR, fix issues, and squash-merge. Returns status string."""
    try:
        pre_fix_sha = git.save_checkpoint()

        # Review the diff
        print("  Running code review...")
        diff = git.diff_full()
        review = review_pr_diff(diff, cfg.repo_path, cfg.review_model)
        telem.record_review(review)

        if not review.has_actionable_issues:
            print("  Review: no critical/medium issues found.")
            return _do_merge(cfg, git, budget, issue, branch, pr_url, sandbox, telem)

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
            telem.record_phase(Phase.REVIEW_FIX, fix_result)
        except RateLimitError:
            raise
        except AgentError as e:
            print(f"  Fix agent failed: {str(e)[:100]}")
            return _do_merge(cfg, git, budget, issue, branch, pr_url, sandbox, telem)

        if fix_result.is_error:
            return _do_merge(cfg, git, budget, issue, branch, pr_url, sandbox, telem)

        # Re-verify after fixes
        print("  Re-verifying after review fixes...")
        verify_results = run_verification(cfg)
        telem.record_verify(verify_results)
        if not all(v.passed for v in verify_results):
            print("  Review fixes broke tests, reverting to original.")
            git.rollback(pre_fix_sha)
            git.push_branch(branch)
            return _do_merge(cfg, git, budget, issue, branch, pr_url, sandbox, telem)

        # Push review fixes
        git.commit_all(f"{commit_prefix(issue)}: address review feedback for #{issue.number}")
        git.push_branch(branch)
        return _do_merge(cfg, git, budget, issue, branch, pr_url, sandbox, telem)

    except RateLimitError:
        raise  # Propagate to stop all processing
    except Exception as e:
        print(f"  Review/merge error: {str(e)[:200]}")
        return _do_merge(cfg, git, budget, issue, branch, pr_url, sandbox, telem)


_CI_ATTEMPT_OUTPUT_MAX = 2000
_CI_ATTEMPT_DIFF_MAX = 3000


def _format_ci_attempt(attempt: int, ci_output: str, diff_summary: str) -> str:
    """Format a single CI fix attempt for context accumulation."""
    return (
        f"## CI Fix Attempt {attempt}\n"
        f"CI output:\n{ci_output[:_CI_ATTEMPT_OUTPUT_MAX]}\n\n"
        f"Changes made:\n{diff_summary[:_CI_ATTEMPT_DIFF_MAX]}\n"
        "---\n\n"
    )


def _run_ci_learn(
    cfg: RunConfig,
    git: GitOps,
    budget: BudgetTracker,
    issue,
    branch: str,
    ci_output: str,
    fix_diff: str,
    sandbox: SandboxConfig,
) -> None:
    """Persist CI fix learnings to the repo's CLAUDE.md (non-critical)."""
    try:
        learn_prompt = build_ci_learn_prompt(ci_output, fix_diff)
        learn_sandbox = build_claude_md_sandbox(cfg)
        learn_budget = min(BUDGET_CI_LEARN, budget.remaining_for_issue_usd(cfg.model))
        learn_result = invoke_agent(
            learn_prompt, cfg.repo_path, cfg.model, cfg.effort,
            learn_budget, learn_sandbox, timeout=TIMEOUT_CLAUDE_MD,
        )
        budget.record(learn_result)
        if not learn_result.is_error:
            try:
                git.commit_all(f"{commit_prefix(issue)}: CI learnings for #{issue.number}")
                git.push_branch(branch)
                print("  CI learnings saved to CLAUDE.md.")
            except RuntimeError:
                pass  # No changes to CLAUDE.md — nothing worth recording
    except Exception:
        pass  # Non-critical — don't fail the pipeline


def _do_merge(
    cfg: RunConfig,
    git: GitOps,
    budget: BudgetTracker,
    issue,
    branch: str,
    pr_url: str,
    sandbox: SandboxConfig,
    telem: Telemetry,
) -> str:
    """Mark PR ready, wait for CI, auto-fix if needed, then merge."""
    mark_ready(cfg.repo_path, pr_url)

    fixes_pushed = 0
    ci_fix_context = ""  # Accumulate context between CI fix attempts

    for ci_attempt in range(1, cfg.max_retries + 1):
        print(f"  Waiting for CI checks (attempt {ci_attempt}, timeout {cfg.ci_timeout}s)...")
        ci_result = wait_for_ci(cfg.repo_path, pr_url, cfg.ci_timeout)

        if ci_result.timed_out:
            print(f"  CI timed out after {cfg.ci_timeout}s. Skipping merge.")
            telem.record_failure(FailureCategory.CI_TIMEOUT)
            return f"PR ready but CI timed out after {cfg.ci_timeout}s: {pr_url}"

        if ci_result.passed:
            if merge_pr(cfg.repo_path, pr_url):
                label = "clean" if fixes_pushed == 0 else f"after {fixes_pushed} CI fix(es)"
                return f"Merged ({label})"
            return f"PR ready, CI passed, but merge failed (may need approval): {pr_url}"

        # CI failed — attempt fix
        print(f"  CI checks failed. Attempting fix ({ci_attempt}/{cfg.max_retries})...")
        telem.record_failure(FailureCategory.CI_FAIL)

        fix_prompt = build_ci_fix_prompt(ci_result.output, previous_attempts=ci_fix_context)
        max_budget = budget.remaining_for_issue_usd(cfg.model)

        try:
            fix_result = invoke_agent(fix_prompt, cfg.repo_path, cfg.model, cfg.effort, max_budget, sandbox)
            budget.record(fix_result)
            telem.record_phase(Phase.CI_FIX, fix_result)
        except RateLimitError:
            raise
        except (AgentError, Exception) as e:
            print(f"  CI fix agent failed: {str(e)[:100]}")
            return f"PR ready but CI fix agent failed: {pr_url}"

        if fix_result.is_error:
            print("  CI fix agent returned error. Skipping merge.")
            return f"PR ready but CI fix agent error: {pr_url}"

        # Re-verify locally before pushing
        print("  Re-verifying after CI fix...")
        verify_results = run_verification(cfg)
        telem.record_verify(verify_results)

        if not all(v.passed for v in verify_results):
            print("  CI fix broke local tests. Skipping merge.")
            return f"PR ready but CI fix broke local verification: {pr_url}"

        try:
            git.commit_all(f"{commit_prefix(issue)}: fix CI for #{issue.number}")
        except RuntimeError:
            print("  CI fix produced no code changes. Cannot fix CI automatically.")
            return f"PR ready but CI fix produced no changes: {pr_url}"

        # Capture what was changed for next attempt's context
        fix_diff = git.diff_last_commit_stats()
        ci_fix_context += _format_ci_attempt(ci_attempt, ci_result.output, fix_diff)

        git.push_branch(branch)
        fixes_pushed += 1

        # Learn from CI fix — persist tribal knowledge to repo's CLAUDE.md
        _run_ci_learn(cfg, git, budget, issue, branch, ci_result.output, fix_diff, sandbox)

        new_sha = git.get_head_sha()
        print(f"  Pushed CI fix. Waiting for checks on {new_sha[:8]}...")
        if not wait_for_new_checks(cfg.repo_path, new_sha):
            print("  Warning: timed out waiting for new check suites. Proceeding anyway.")

    # Final CI check after last fix
    if fixes_pushed > 0:
        print(f"  Final CI check (timeout {cfg.ci_timeout}s)...")
        ci_result = wait_for_ci(cfg.repo_path, pr_url, cfg.ci_timeout)
        if ci_result.passed:
            if merge_pr(cfg.repo_path, pr_url):
                return f"Merged (after {fixes_pushed} CI fix(es))"
            return f"PR ready, CI passed, but merge failed (may need approval): {pr_url}"

    return f"PR ready but CI failed after {cfg.max_retries} attempts: {pr_url}"


def _fmt_tok(n: int) -> str:
    """Format token count compactly: 1.2M, 450K, 3.8K, 800."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def _print_run_summary(telem: Telemetry, log: RunLogger, budget: "BudgetTracker") -> None:
    s = telem.run_summary(
        daily_tokens_used=budget.daily_tokens_used,
        daily_cap_tokens=budget.daily_cap_tokens,
    )
    w = 58
    print(f"\n{'=' * w}")
    print(f" AutoCoder Run Summary ({log.run_id})")
    print(f"{'=' * w}")
    print(f"  Issues: {s.issues_processed} | PRs: {s.success_count} | Retries: {s.retry_count} | Skipped: {s.skip_count}")
    print(f"  Total cost: ${s.total_cost_usd:.4f} | Cache hit: {s.overall_cache_hit_rate:.1%}")
    print(f"  Tokens: {_fmt_tok(s.total_tokens_in)} in | {_fmt_tok(s.total_tokens_out)} out | {_fmt_tok(s.total_tokens_cached)} cached")
    if s.daily_cap_tokens > 0:
        pct = s.daily_tokens_used / s.daily_cap_tokens * 100
        remaining = max(s.daily_cap_tokens - s.daily_tokens_used, 0)
        print(f"  Budget: {pct:.0f}% used ({_fmt_tok(s.daily_tokens_used)} / {_fmt_tok(s.daily_cap_tokens)}) | {_fmt_tok(remaining)} remaining")

    if s.phase_token_detail:
        lw = max(len(p) for p in s.phase_token_detail)
        print(f"\n  {'Phase':<{lw+2}} {'in':>6} {'out':>6} {'cached':>6}     {'cost':>8}")
        for phase, cost in sorted(s.phase_cost_breakdown.items(), key=lambda x: -x[1]):
            ti, to, tc = s.phase_token_detail.get(phase, (0, 0, 0))
            print(f"    {phase:<{lw}} {_fmt_tok(ti):>6} {_fmt_tok(to):>6} {_fmt_tok(tc):>6}   ${cost:>8.4f}")

    if s.per_model_tokens:
        lw = max(len(m) for m in s.per_model_tokens)
        print(f"\n  {'Model':<{lw+2}} {'in':>6} {'out':>6} {'cached':>6}     {'cost':>8}")
        for model, cost in sorted(s.per_model_cost.items(), key=lambda x: -x[1]):
            ti, to, tc = s.per_model_tokens.get(model, (0, 0, 0))
            print(f"    {model:<{lw}} {_fmt_tok(ti):>6} {_fmt_tok(to):>6} {_fmt_tok(tc):>6}   ${cost:>8.4f}")

    if s.per_issue_summary:
        print(f"\n  {'Issue':<8} {'in':>6} {'out':>6} {'cached':>6}     {'cost':>8}")
        for inum, (ti, to, tc, cost) in sorted(s.per_issue_summary.items(), key=lambda x: -x[1][3]):
            print(f"    #{inum:<6} {_fmt_tok(ti):>6} {_fmt_tok(to):>6} {_fmt_tok(tc):>6}   ${cost:>8.4f}")

    if s.top_failure_reasons:
        reasons = " ".join(f"{r}({c})" for r, c in s.top_failure_reasons)
        print(f"\n  Top Failures: {reasons}")

    print(f"\n  Log: {log.log_path}")
    print(f"{'=' * w}\n")
