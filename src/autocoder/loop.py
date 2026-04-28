from __future__ import annotations

import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from pathlib import Path

from autocoder.agent import (
    build_prompt, build_plan_prompt, build_implement_prompt,
    build_task_plan_prompt, build_task_execute_prompt,
    build_update_claude_md_prompt, build_ci_learn_prompt, build_impl_learn_prompt,
    generate_implement_brief,
    invoke_agent, set_rate_limit_wait, set_timeouts,
    TIMEOUT_PLAN, TIMEOUT_IMPLEMENT, TIMEOUT_BUILD_FIX,
    TIMEOUT_CLAUDE_MD, BUDGET_CLAUDE_MD, BUDGET_CI_LEARN, BUDGET_IMPL_LEARN,
)
from autocoder import task_slice
from autocoder.anticheat import audit_diff, protect_test_files, restore_test_files
from autocoder.budget import BudgetTracker
from autocoder.git import GitOps
from autocoder.epic import process_epic
from autocoder.issues import analyze_and_prioritize, fetch_issues, fetch_issues_by_number, parse_sub_issues
from autocoder.logger import RunLogger
from autocoder.pr import comment_failure, create_pr, label_failed, mark_ready, merge_pr, wait_for_ci, wait_for_new_checks
from autocoder.review import (
    build_build_fix_prompt, build_ci_fix_prompt, build_ci_fix_arch_prompt,
    build_fix_prompt, merge_reviews, review_and_fix_multi, review_pr_diff,
    run_external_review,
)
from autocoder.sandbox import SandboxConfig, build_sandbox, build_brief_sandbox, build_plan_sandbox, build_claude_md_sandbox, build_review_sandbox
from autocoder.server import DashboardServer, EventBus
from autocoder.testplan import (
    build_test_plan_fix_prompt,
    extract_acceptance_criteria,
    verify_test_plan,
)
from autocoder.telemetry import FailureCategory, Phase, Telemetry
from autocoder.types import (
    AgentError,
    AgentResult,
    AntiCheatViolation,
    AuthenticationError,
    IdleTimeoutError,
    ImplementerBlockedError,
    ImplementerStatus,
    Outcome,
    RateLimitError,
    ReviewResult,
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


class StalemateTracker:
    """Track consecutive iterations with no SHA change. Reports stalemate when threshold is hit."""

    def __init__(self, threshold: int = 2) -> None:
        self._threshold = threshold
        self._streak = 0

    def note(self, prev_sha: str, new_sha: str) -> bool:
        if prev_sha == new_sha:
            self._streak += 1
        else:
            self._streak = 0
        return self._streak >= self._threshold

    @property
    def streak(self) -> int:
        return self._streak


class StepTimings:
    def __init__(self) -> None:
        self._steps: list[StepTiming] = []
        self._lock = threading.Lock()

    def record(self, name: str, duration_ms: int) -> None:
        with self._lock:
            self._steps.append(StepTiming(name, duration_ms))

    @property
    def steps(self) -> list[StepTiming]:
        with self._lock:
            return list(self._steps)

    @property
    def total_ms(self) -> int:
        with self._lock:
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

    dashboard: DashboardServer | None = None
    event_bus: EventBus | None = None
    if cfg.serve:
        event_bus = EventBus()
        dashboard = DashboardServer(event_bus, port=cfg.port)
        actual_port = dashboard.start()
        print(f"Dashboard: http://127.0.0.1:{actual_port}")

    telem = Telemetry(event_bus=event_bus)

    set_rate_limit_wait(cfg.rate_limit_wait_seconds)
    set_timeouts(cfg.idle_timeout_seconds, cfg.session_timeout_seconds)

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

        # Process regular issues: sequentially (parallel=1) or in a worker pool
        if cfg.parallel > 1 and regular_issues:
            _process_issues_parallel(regular_issues, cfg, git, budget, log, timings, telem)
        else:
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
        if dashboard is not None:
            dashboard.stop()


_BLOCKING_STATUSES = {ImplementerStatus.BLOCKED, ImplementerStatus.NEEDS_CONTEXT}


def _handle_implementer_status(
    agent_result: AgentResult, original_prompt: str, cfg: RunConfig,
    sandbox: SandboxConfig, budget: BudgetTracker, telem: Telemetry,
    tag: str, att: str, timings: StepTimings,
) -> AgentResult:
    """React to STATUS: line in implementer reply.

    BLOCKED / NEEDS_CONTEXT → if --escalate-on-block, retry once with the
    stronger escalation model and the implementer's stated detail prepended as
    additional context. If the retry still reports BLOCKED/NEEDS_CONTEXT,
    raise ImplementerBlockedError so the outer attempt loop counts it as a
    failure.

    DONE_WITH_CONCERNS → log + emit telemetry, then proceed.
    DONE / unknown → no-op.
    """
    status = agent_result.status
    detail = (agent_result.status_detail or "").strip()
    issue_num = telem._get_current().issue_number if telem._get_current() else 0

    if status == ImplementerStatus.DONE_WITH_CONCERNS:
        print(f"  Implementer DONE_WITH_CONCERNS: {detail[:200]}")
        telem.emit("implementer_concerns", issue=issue_num, detail=detail[:500])
        return agent_result

    if status not in _BLOCKING_STATUSES:
        return agent_result

    if not cfg.escalate_on_block:
        raise ImplementerBlockedError(
            f"Implementer reported {status.value}: {detail[:300]}"
        )

    print(
        f"  Implementer {status.value}: {detail[:200]}\n"
        f"  Escalating to {cfg.escalation_model}..."
    )
    telem.emit(
        "implementer_escalation",
        issue=issue_num, status=status.value, detail=detail[:500],
        from_model=cfg.model, to_model=cfg.escalation_model,
    )

    escalation_prompt = (
        f"{original_prompt}\n\n"
        "--- PRIOR ATTEMPT REPORTED A BLOCKING STATUS ---\n"
        f"Prior model: {cfg.model}\n"
        f"Prior status: {status.value}\n"
        f"Prior detail: {detail or '(no detail provided)'}\n"
        "You are a stronger model picking this up. Address the blocker "
        "explicitly: if it was missing context, infer it from the codebase; "
        "if it was architectural ambiguity, pick the simplest viable path "
        "and document why. Do NOT report the same blocking status again."
        "\n--- END PRIOR ATTEMPT ---\n"
    )

    with StepTimer(f"agent_escalation {tag} {att}", timings):
        max_budget = budget.remaining_for_issue_usd(cfg.escalation_model)
        escalated = invoke_agent(
            escalation_prompt, cfg.repo_path, cfg.escalation_model, cfg.effort,
            max_budget, sandbox,
        )
    budget.record(escalated)
    telem.record_phase(Phase.IMPLEMENT, escalated)

    if escalated.is_error:
        raise AgentError(escalated.result_text)

    if escalated.status in _BLOCKING_STATUSES:
        raise ImplementerBlockedError(
            f"Escalation to {cfg.escalation_model} also reported "
            f"{escalated.status.value}: {(escalated.status_detail or '')[:300]}"
        )

    if escalated.status == ImplementerStatus.DONE_WITH_CONCERNS:
        print(f"  Escalation DONE_WITH_CONCERNS: {(escalated.status_detail or '')[:200]}")
        telem.emit(
            "implementer_concerns", issue=issue_num,
            detail=(escalated.status_detail or "")[:500], escalated=True,
        )

    return escalated


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
        telem.begin_issue(issue.number, attempt, title=issue.title)
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

            # Stage 3a (optional): pre-implement design brief
            brief_text = ""
            if cfg.implement_brief:
                with StepTimer(f"brief {tag} {att}", timings):
                    brief_sandbox = build_brief_sandbox(cfg)
                    brief_budget = min(
                        cfg.brief_budget_usd, budget.remaining_for_issue_usd(cfg.model),
                    )
                    brief_result = generate_implement_brief(
                        issue, cfg.repo_path, cfg.model, cfg.effort,
                        brief_budget, brief_sandbox,
                    )
                budget.record(brief_result)
                telem.record_phase(Phase.IMPLEMENT_BRIEF, brief_result)
                if brief_result.is_error:
                    print(f"  Brief generation failed, continuing without brief: {brief_result.result_text[:100]}")
                else:
                    brief_text = brief_result.result_text

            # Decide execution mode: task-sliced (fresh context per task) vs monolithic.
            use_task_slice = task_slice.should_task_slice(issue, cfg)
            task_slice_fallback_ctx = ""
            if use_task_slice:
                with StepTimer(f"task_slice {tag} {att}", timings):
                    ok, ts_err, ts_result = _run_task_slice(
                        issue, cfg, git, budget, telem, brief_text, sandbox,
                    )
                if ok:
                    agent_result = ts_result
                else:
                    print(f"  Task-slice fallback: {ts_err[:150]}")
                    git.rollback(checkpoint)
                    subprocess.run(
                        ["git", "clean", "-fd"], cwd=cfg.repo_path,
                        check=False, capture_output=True,
                    )
                    use_task_slice = False
                    task_slice_fallback_ctx = (
                        f"Prior task-slice attempt failed: {ts_err[:300]}"
                    )

            if not use_task_slice:
                # Monolithic implement path.
                mono_err_ctx = error_context
                if task_slice_fallback_ctx and not mono_err_ctx:
                    mono_err_ctx = task_slice_fallback_ctx

                if cfg.plan_mode and plan_sandbox:
                    # Phase 1: Plan (read-only)
                    with StepTimer(f"plan {tag} {att}", timings):
                        plan_prompt = build_plan_prompt(issue, cfg.repo_path)
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

                    # Phase 2: Implement with plan context (+ brief if present)
                    prompt = build_implement_prompt(
                        issue, plan_text, mono_err_ctx, cfg.repo_path, brief=brief_text,
                    )
                else:
                    prompt = build_prompt(
                        issue,
                        error_context=mono_err_ctx,
                        repo_path=cfg.repo_path,
                        brief=brief_text,
                    )

                with StepTimer(f"agent {tag} {att}", timings):
                    max_budget = budget.remaining_for_issue_usd(cfg.model)
                    agent_result = invoke_agent(prompt, cfg.repo_path, cfg.model, cfg.effort, max_budget, sandbox)
                budget.record(agent_result)
                telem.record_phase(Phase.IMPLEMENT, agent_result)

                if agent_result.is_error:
                    raise AgentError(agent_result.result_text)

                agent_result = _handle_implementer_status(
                    agent_result, prompt, cfg, sandbox, budget, telem, tag, att, timings,
                )

            # Restore test files before verification
            if protected_files:
                restore_test_files(protected_files)
                protected_files = []

            # Anti-cheat audit
            if cfg.protect_tests:
                with StepTimer(f"anticheat {tag}", timings):
                    audit_diff(cfg.repo_path, cfg.test_patterns)

            # Stage 3.5 (optional): pre-verify critique (two-stage shift-left review)
            if cfg.pre_verify_critique:
                with StepTimer(f"pre_verify_critique {tag} {att}", timings):
                    diff_for_critique = git.diff_full()
                    critique_sandbox = build_review_sandbox(cfg)
                    critique_budget = min(
                        cfg.pre_verify_budget_usd, budget.remaining_for_issue_usd(cfg.model),
                    )
                    critique_outcome, _ = review_and_fix_multi(
                        diff_for_critique, cfg.repo_path, cfg.model,
                        critique_sandbox, critique_budget,
                        issue_body=issue.body,
                        telem=telem, budget_tracker=budget,
                        spec_phase=Phase.PRE_VERIFY_CRITIQUE,
                        quality_phase=Phase.PRE_VERIFY_CRITIQUE,
                    )
                print(f"  Pre-verify critique: {critique_outcome.summary}")
                if critique_outcome.failed:
                    error_context = (
                        f"Pre-verify critique reported unfixable issues: {critique_outcome.summary}"
                    )
                    raise VerificationError("pre_verify_critique", error_context)

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
                        fix_prompt = build_build_fix_prompt(format_failure(failed), cfg.build_cmd or "", cfg.repo_path)
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
                        fix_prompt = build_test_plan_fix_prompt(issue, failed_items, cfg.repo_path)
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
                        md_prompt = build_update_claude_md_prompt(diff_for_md, existing_content, cfg.repo_path)
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
                    learn_prompt = build_impl_learn_prompt(impl_diff, verify_summary, cfg.repo_path)
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
            elif isinstance(e, IdleTimeoutError):
                telem.record_failure(FailureCategory.IDLE_TIMEOUT)
                telem.emit("idle", issue=issue.number, reason=str(e))
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
                    if isinstance(e, IdleTimeoutError):
                        error_context = (
                            f"Previous attempt hung silently ({str(e)}). "
                            "Make visible progress early — prefer small incremental edits and avoid long silent thinking."
                        )
                    else:
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


def _default_worktree_root(cfg: RunConfig) -> Path:
    if cfg.worktree_root:
        return Path(cfg.worktree_root)
    return Path(cfg.repo_path) / ".autocoder" / "worktrees"


def _process_issues_parallel(
    regular_issues: list, cfg: RunConfig, main_git: GitOps,
    budget: BudgetTracker, log: RunLogger, timings: StepTimings, telem: Telemetry,
) -> None:
    """Run `process_issue` concurrently in per-issue worktrees. Stops submitting
    new work when any worker raises RateLimitError / AuthenticationError.
    """
    root = _default_worktree_root(cfg)
    root.mkdir(parents=True, exist_ok=True)
    # Prune any stale worktree admin records from previous runs
    main_git.prune_worktrees()

    stop_event = threading.Event()

    def _worker(issue) -> str:
        if stop_event.is_set():
            return "skipped"
        if budget.daily_exhausted():
            return "daily_cap"

        wt_path = root / f"issue-{issue.number}"
        # Clear any leftover from a prior failed run
        if wt_path.exists():
            main_git.remove_worktree(str(wt_path))

        wt_branch = f"autocoder-wt-{log.run_id}-{issue.number}"
        try:
            main_git.create_worktree(str(wt_path), wt_branch, base=main_git.get_main_branch())
        except subprocess.CalledProcessError as e:
            print(f"  [#{issue.number}] worktree create failed: {e.stderr[:200] if e.stderr else e}")
            return "worktree_error"

        worker_git = GitOps(str(wt_path))
        try:
            print(f"  [#{issue.number}] start in {wt_path}")
            process_issue(issue, cfg, worker_git, budget, log, timings, telem)
            return "success"
        except RateLimitError as e:
            stop_event.set()
            log.log_event("rate_limited", error=str(e), issue=issue.number)
            print(f"  [#{issue.number}] Rate limit hit. Stopping submission.")
            raise
        except AuthenticationError as e:
            stop_event.set()
            log.log_event("auth_failed", error=str(e), issue=issue.number)
            print(f"  [#{issue.number}] Authentication failed. Stopping submission.")
            raise
        except Exception as e:
            print(f"  [#{issue.number}] unhandled worker error: {str(e)[:200]}")
            return "error"
        finally:
            try:
                main_git.remove_worktree(str(wt_path))
            except Exception:
                pass

    with ThreadPoolExecutor(max_workers=cfg.parallel) as pool:
        futures = {pool.submit(_worker, iss): iss for iss in regular_issues}
        try:
            for fut in as_completed(futures):
                try:
                    fut.result()
                except (RateLimitError, AuthenticationError):
                    # stop_event already set; cancel any not-yet-started futures
                    for f in futures:
                        if not f.done():
                            f.cancel()
                    break
        finally:
            main_git.prune_worktrees()


def _run_task_slice(
    issue, cfg: RunConfig, git: GitOps, budget: BudgetTracker, telem: Telemetry,
    brief_text: str, sandbox: SandboxConfig,
) -> tuple[bool, str, AgentResult | None]:
    """Run the task-sliced implement flow.

    Generates a plan file, then executes each `- [ ]` task in a fresh Claude
    subprocess until all are marked `- [x]`. Returns (success, err_ctx, last_result).
    On failure, caller is expected to roll back the working tree.
    """
    plan_p = task_slice.plan_path(cfg.repo_path, issue.number)
    plan_p.parent.mkdir(parents=True, exist_ok=True)
    # Ensure stale plan doesn't bleed into this attempt
    if plan_p.exists():
        try:
            plan_p.unlink()
        except OSError:
            pass

    plan_prompt = build_task_plan_prompt(issue, str(plan_p), brief_text, cfg.repo_path)
    max_budget = budget.remaining_for_issue_usd(cfg.model)
    plan_result = invoke_agent(
        plan_prompt, cfg.repo_path, cfg.model, cfg.effort, max_budget, sandbox,
    )
    budget.record(plan_result)
    telem.record_phase(Phase.TASK_PLAN, plan_result)

    if plan_result.is_error or not plan_p.exists():
        telem.record_failure(FailureCategory.TASK_PLAN_FAIL)
        return False, f"Plan generation failed: {plan_result.result_text[:300]}", plan_result

    # Placeholder lint — reject under-specified plans, regenerate once.
    violations = task_slice.validate_plan(plan_p.read_text())
    if violations:
        print(f"  Plan placeholder lint: {len(violations)} violation(s); regenerating once.")
        violations_msg = "\n".join(f"- {v}" for v in violations)
        regen_prompt = (
            f"{plan_prompt}\n\n"
            "--- PRIOR PLAN REJECTED ---\n"
            "Your previous draft contained placeholder phrases. Rewrite the\n"
            "plan and replace each of the following with the actual content:\n"
            f"{violations_msg}\n"
            "--- END REJECTION ---\n"
        )
        max_budget = budget.remaining_for_issue_usd(cfg.model)
        plan_result = invoke_agent(
            regen_prompt, cfg.repo_path, cfg.model, cfg.effort, max_budget, sandbox,
        )
        budget.record(plan_result)
        telem.record_phase(Phase.TASK_PLAN, plan_result)
        if plan_result.is_error or not plan_p.exists():
            telem.record_failure(FailureCategory.TASK_PLAN_FAIL)
            return False, f"Plan regeneration failed: {plan_result.result_text[:300]}", plan_result
        violations = task_slice.validate_plan(plan_p.read_text())
        if violations:
            telem.record_failure(FailureCategory.TASK_PLAN_FAIL)
            print(f"  Plan still has {len(violations)} placeholder violation(s) after regeneration; falling back to monolithic implement.")
            return False, "Plan placeholders persisted after regeneration.", plan_result

    tasks = task_slice.parse_plan(plan_p.read_text())
    if not tasks:
        telem.record_failure(FailureCategory.TASK_PLAN_FAIL)
        return False, "Plan had no parseable `- [ ]` tasks.", plan_result
    if len(tasks) > cfg.max_tasks:
        telem.record_failure(FailureCategory.TASK_PLAN_FAIL)
        return False, f"Plan has {len(tasks)} tasks; --max-tasks is {cfg.max_tasks}.", plan_result

    print(f"  Task plan: {len(tasks)} tasks across fresh sessions.")
    telem.emit("task_plan", issue=issue.number, total=len(tasks))

    last_result: AgentResult = plan_result
    # Cap loop iterations as a safety bound against checkbox-lying subprocesses
    for _ in range(cfg.max_tasks * (cfg.task_retries + 1) + 1):
        tasks = task_slice.parse_plan(plan_p.read_text())
        current = task_slice.next_task(tasks)
        if current is None:
            break

        task_err = ""
        task_success = False
        for _attempt in range(cfg.task_retries + 1):
            exec_prompt = build_task_execute_prompt(
                issue, str(plan_p), current.text,
                error_context=task_err, repo_path=cfg.repo_path,
            )
            max_budget = budget.remaining_for_issue_usd(cfg.model)
            t_result = invoke_agent(
                exec_prompt, cfg.repo_path, cfg.model, cfg.effort, max_budget, sandbox,
            )
            budget.record(t_result)
            telem.record_phase(Phase.TASK_EXEC, t_result)
            last_result = t_result

            if t_result.is_error:
                task_err = t_result.result_text[:500] or "agent error"
                continue

            after = task_slice.parse_plan(plan_p.read_text())
            if len(after) >= current.index and after[current.index - 1].done:
                task_success = True
                telem.emit(
                    "task_exec", issue=issue.number,
                    index=current.index, text=current.text[:120],
                    done_count=task_slice.done_count(after), total=len(after),
                )
                break

            task_err = (
                f"Task #{current.index} was not marked [x] in {plan_p}. "
                "Edit the plan file to flip this task's `- [ ]` to `- [x]` after making the code change."
            )

        if not task_success:
            telem.record_failure(FailureCategory.TASK_EXEC_FAIL)
            return False, f"Task #{current.index} failed: {task_err[:300]}", last_result

    # Build a human-readable summary from the completed plan for PR body
    final_tasks = task_slice.parse_plan(plan_p.read_text())
    summary_lines = ["Completed tasks:"]
    for t in final_tasks:
        summary_lines.append(f"- [{'x' if t.done else ' '}] {t.text}")
    last_result.result_text = "\n".join(summary_lines)[:1000]
    return True, "", last_result


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

        # Route to multi-agent review if configured. The orchestrator fixes
        # issues in-session, so we skip the separate fix-agent step below.
        if cfg.review_mode == "multi":
            return _multi_review_and_merge(
                cfg, git, budget, issue, branch, pr_url, sandbox, telem, pre_fix_sha,
            )

        # Review the diff (primary + optional external, run in parallel)
        print("  Running code review...")
        diff = git.diff_full()
        review = _run_reviews_parallel(cfg, diff, telem)
        telem.record_review(review)

        if not review.has_actionable_issues:
            print("  Review: no critical/medium issues found.")
            return _do_merge(cfg, git, budget, issue, branch, pr_url, sandbox, telem)

        # Log findings
        for f in review.findings:
            print(f"  Review [{f.severity.upper()}] {f.file}: {f.description}")

        # Fix issues
        print(f"  Fixing {len(review.findings)} review issue(s)...")
        fix_prompt = build_fix_prompt(review.findings, cfg.repo_path)
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


def _run_reviews_parallel(
    cfg: RunConfig, diff: str, telem: Telemetry,
) -> ReviewResult:
    """Run primary claude review and optional external reviewer in parallel,
    merge results. Returns merged ReviewResult."""
    def _primary() -> ReviewResult:
        return review_pr_diff(diff, cfg.repo_path, cfg.review_model)

    def _external() -> ReviewResult | None:
        if not cfg.external_reviewer_cmd:
            return None
        label = cfg.external_reviewer_cmd[0]
        result, duration_ms = run_external_review(diff, cfg.external_reviewer_cmd, cfg.repo_path, label)
        # Record a minimal AgentResult for telemetry (cost unknown for external tools)
        telem.record_phase(Phase.REVIEW_EXTERNAL, AgentResult(
            session_id="external", result_text=result.raw_response[:500],
            is_error=False, duration_ms=duration_ms,
            tokens_in=0, tokens_out=0, tokens_cached=0, cost_usd=0.0,
            num_turns=1, model=label,
        ))
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        primary_fut = pool.submit(_primary)
        external_fut = pool.submit(_external)
        primary = primary_fut.result()
        external = external_fut.result()

    if external is not None and external.findings:
        print(f"  External reviewer ({cfg.external_reviewer_cmd[0]}) added {len(external.findings)} finding(s).")
        return merge_reviews(primary, external)
    return primary


def _multi_review_and_merge(
    cfg: RunConfig,
    git: GitOps,
    budget: BudgetTracker,
    issue,
    branch: str,
    pr_url: str,
    sandbox: SandboxConfig,
    telem: Telemetry,
    pre_fix_sha: str,
) -> str:
    """Run the multi-agent orchestrator (fixes in-session), verify, push, merge."""
    try:
        print("  Running multi-agent code review (5 parallel reviewers)...")
        review_sandbox = build_review_sandbox(cfg)
        diff = git.diff_full()

        # Optionally run external reviewer in parallel and pass its findings
        # as context to the orchestrator.
        external = None
        if cfg.external_reviewer_cmd:
            label = cfg.external_reviewer_cmd[0]
            print(f"  External reviewer ({label}) running in parallel...")
            external, ext_duration_ms = run_external_review(
                diff, cfg.external_reviewer_cmd, cfg.repo_path, label,
            )
            telem.record_phase(Phase.REVIEW_EXTERNAL, AgentResult(
                session_id="external", result_text=external.raw_response[:500],
                is_error=False, duration_ms=ext_duration_ms,
                tokens_in=0, tokens_out=0, tokens_cached=0, cost_usd=0.0,
                num_turns=1, model=label,
            ))

        budget_cap = min(cfg.review_budget_usd, budget.remaining_for_issue_usd(cfg.review_model))
        outcome, _ = review_and_fix_multi(
            diff, cfg.repo_path, cfg.review_model, review_sandbox, budget_cap,
            external=external,
            issue_body=issue.body,
            telem=telem, budget_tracker=budget,
            spec_phase=Phase.REVIEW_SPEC_COMPLIANCE,
            quality_phase=Phase.REVIEW_QUALITY,
        )
        print(f"  Review outcome: {outcome.summary}")

        if outcome.failed:
            if "QUALITY_FAILED" in outcome.summary:
                telem.record_failure(FailureCategory.QUALITY_REVIEW_FAILED)
            elif "SPEC_FAILED" in outcome.summary:
                telem.record_failure(FailureCategory.SPEC_COMPLIANCE_FAILED)
            else:
                telem.record_failure(FailureCategory.REVIEW_REJECTED)
            return f"PR ready but review orchestrator reported unfixable issues: {pr_url}"

        # If the orchestrator cleaned up, re-verify before merging
        if git.get_head_sha() != pre_fix_sha:
            print("  Re-verifying after orchestrator fixes...")
            verify_results = run_verification(cfg)
            telem.record_verify(verify_results)
            if not all(v.passed for v in verify_results):
                print("  Orchestrator fixes broke tests, reverting.")
                git.rollback(pre_fix_sha)
                git.push_branch(branch)
                return _do_merge(cfg, git, budget, issue, branch, pr_url, sandbox, telem)

            try:
                git.commit_all(f"{commit_prefix(issue)}: address review feedback for #{issue.number}")
            except RuntimeError:
                # Orchestrator edited working tree but nothing was added/kept
                pass
            git.push_branch(branch)

        return _do_merge(cfg, git, budget, issue, branch, pr_url, sandbox, telem)

    except RateLimitError:
        raise
    except Exception as e:
        print(f"  Multi-agent review error: {str(e)[:200]}")
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
        learn_prompt = build_ci_learn_prompt(ci_output, fix_diff, cfg.repo_path)
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


_CI_ARCH_TIMEOUT = 600  # 10 minutes — analysis-only, single sub-agent
_CI_ARCH_BUDGET = 0.50  # cents — capped; arch review is read-only and short


def _run_ci_arch_review(
    cfg: RunConfig, budget: BudgetTracker, telem: Telemetry,
    ci_output: str, prior_attempts: str,
) -> str:
    """Run the analysis-only architectural critique; return the recommendation
    text (empty on failure / disabled / budget exhausted). Read-only sandbox.
    """
    arch_budget = min(_CI_ARCH_BUDGET, budget.remaining_for_issue_usd(cfg.review_model))
    if arch_budget < 0.05:
        return ""
    arch_prompt = build_ci_fix_arch_prompt(ci_output, prior_attempts, cfg.repo_path)
    arch_sandbox = build_plan_sandbox(cfg)
    print("  Stalemate detected — running architectural critique (analysis-only)...")
    try:
        arch_result = invoke_agent(
            arch_prompt, cfg.repo_path, cfg.review_model, cfg.effort,
            arch_budget, arch_sandbox, timeout=_CI_ARCH_TIMEOUT,
        )
    except (RateLimitError, AuthenticationError):
        raise
    except Exception as e:
        print(f"  Arch critique skipped: {str(e)[:100]}")
        return ""
    budget.record(arch_result)
    telem.record_phase(Phase.CI_FIX_ARCH, arch_result)
    if arch_result.is_error:
        print("  Arch critique returned an error; skipping.")
        return ""
    head = arch_result.result_text[:400].replace("\n", " ")
    print(f"  Arch critique: {head}...")
    return arch_result.result_text


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
    stalemate = StalemateTracker(cfg.stalemate_threshold)
    last_sha = git.get_head_sha()

    for ci_attempt in range(1, cfg.max_retries + 1):
        print(f"  Waiting for CI checks (attempt {ci_attempt}, timeout {cfg.ci_timeout}s)...")
        ci_result = wait_for_ci(cfg.repo_path, pr_url, cfg.ci_timeout)
        telem.emit(
            "ci", issue=issue.number, attempt=ci_attempt,
            passed=ci_result.passed, timed_out=ci_result.timed_out,
        )

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

        # When the stalemate tracker is at threshold-1, prior fixes have not
        # changed the head SHA — symptom-patching has stalled. Run an
        # analysis-only architectural critique to inform this attempt.
        arch_recommendation = ""
        if (
            cfg.ci_arch_review and ci_fix_context and
            stalemate.streak >= max(cfg.stalemate_threshold - 1, 1)
        ):
            arch_recommendation = _run_ci_arch_review(
                cfg, budget, telem, ci_result.output, ci_fix_context,
            )

        fix_prompt = build_ci_fix_prompt(ci_result.output, previous_attempts=ci_fix_context, repo_path=cfg.repo_path)
        if arch_recommendation:
            fix_prompt = (
                "## Architectural critique from prior-attempts analysis\n\n"
                f"{arch_recommendation}\n\n"
                "Use this context when proposing the fix below. If the critique "
                "recommends ESCALATE or REFACTOR beyond the scope of one fix, "
                "make the smallest viable change and clearly document the "
                "remaining gap in your commit message.\n\n"
                "---\n\n"
            ) + fix_prompt
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
            telem.record_failure(FailureCategory.CI_STALEMATE)
            return f"PR ready but CI fix produced no changes: {pr_url}"

        # Capture what was changed for next attempt's context
        fix_diff = git.diff_last_commit_stats()
        ci_fix_context += _format_ci_attempt(ci_attempt, ci_result.output, fix_diff)

        new_sha_after_commit = git.get_head_sha()
        if stalemate.note(last_sha, new_sha_after_commit):
            print(f"  CI fix stalemate: no SHA change in {cfg.stalemate_threshold} attempts. Stopping.")
            telem.record_failure(FailureCategory.CI_STALEMATE)
            return f"PR ready but CI fix stalemated: {pr_url}"
        last_sha = new_sha_after_commit

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
