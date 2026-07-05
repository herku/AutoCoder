# AutoCoder

Autonomous AI coding agent that resolves GitHub issues end-to-end: fetch, prioritize, branch, brief, implement, critique, verify, PR, review, merge.

## Setup

```bash
uv sync                  # install deps (Python 3.11+, Click 8.0+)
uv run pytest            # run tests
uv run autocoder --repo /path/to/repo --test-cmd "npm test" --lint-cmd "npm run lint"
```

Requires: Claude Code CLI + GitHub CLI, both authenticated.

## Architecture

```
cli.py          Click entry point, all CLI options
config.py       RunConfig builder; Docker image age checks; duration parsing; external-reviewer preset resolution
loop.py         Main orchestration: fetch → prioritize → brief → implement → critique → verify → PR → review/merge; StalemateTracker; worktree-parallel worker pool (`--parallel N`); task-slice dispatch
agent.py        Invokes Claude Code CLI subprocess via Popen+watchdog (idle/session timeouts); rate-limit/auth detection; rate-limit wait+retry; brief/task/learn prompt builders
build.py        AI-powered build command detection (claude -p) with heuristic fallback
sandbox.py      SandboxConfig: per-phase scoped allowed tools (implement/plan/brief/review/claude_md/detect)
issues.py       GitHub CLI wrapper: fetch/parse issues, extract acceptance criteria, priority caching
review.py       Single-reviewer + multi-agent orchestrator; external-reviewer subprocess; fix/ci_fix/build_fix prompt builders
testplan.py     Acceptance criteria verification against diff
verify.py       Runs lint/unit/integration/build tests (stops on first failure)
budget.py       Token & cost tracking (per-issue + daily cap); thread-local per-issue tokens, locked daily totals
git.py          GitOps class: branching, commit, diff, rollback, merge, lockfile, worktree create/remove/prune
anticheat.py    Protect-tests mode: read-only test files, audit violations
logger.py       Thread-safe JSONL logging + dead-letter queue for failures
telemetry.py    Thread-safe per-phase cost/token tracking; Phase enum incl. IMPLEMENT_BRIEF, PRE_VERIFY_CRITIQUE, REVIEW_MULTI, REVIEW_EXTERNAL, TASK_PLAN, TASK_EXEC; optional event-bus pub/sub for dashboard
server.py       Localhost SSE dashboard (`--serve`): EventBus + ThreadingHTTPServer + bundled HTML
task_slice.py   Ralph-loop-style fresh-context-per-task: heuristic, plan parse, checkbox walk
epic.py         Epic/meta-issue support: process sub-issues, track progress, close parent
pr.py           PR creation, readiness, CI wait, merge
types.py        Dataclasses: Issue, AgentResult, VerifyResult, RunConfig, ReviewResult, MultiReviewResult, EpicResult, TaskItem, exceptions (incl. IdleTimeoutError)
prompts/        Markdown templates loaded via prompts.load(name, repo_path); supports repo overrides + {{agent:X}} expansion
prompts/agents/ Role briefs for brief advisors (architecture/tests/risks) and review agents (quality/implementation/testing/simplification/documentation)
```

## Pipeline Phases (per issue)

1. Branch creation: `feat/{num}-{slug}` from main (or per-worker worktree under `.autocoder/worktrees/` when `--parallel N`)
2. Plan phase (optional `--plan-mode`): read-only analysis
3. Pre-implement brief (default on, `--no-implement-brief` to disable): orchestrator spawns 3 parallel advisors (architecture/tests/risks), synthesizes a compact design brief prepended to implementer prompt
4. Implement phase:
    - Default: single long Claude session (monolithic)
    - Task-sliced (via `--task-slice` or auto-heuristic: ≥3 acceptance-criteria checkboxes OR body >1500 chars): orchestrator writes `.autocoder/plan-<N>.md` with `- [ ]` task checklist; then each task runs in a fresh Claude subprocess that marks its checkbox done. Falls back to monolithic on plan/task failure within the same attempt.
5. Anti-cheat audit (if `--protect-tests`): reject if test files modified
6. Pre-verify critique (default on, `--no-pre-verify-critique` to disable): multi-agent shift-left review on staged diff; fixes in-session or raises VerificationError on unfixable findings
7. Verification: lint -> unit -> integration -> build (stop on first failure)
   7a. In-place fix (any stage fails): build → build_fix prompt (separate `--build-retries` budget); lint/unit/integration → verify_fix prompt (`--verify-fix`, default on) — one focused fix + re-verify before falling back to rollback+retry
8. Test plan verification: check acceptance criteria checkboxes against diff; fix + re-verify if any fail; post-fix affirmative fails GATE the attempt (`--testplan-enforce`, default on; verifier infrastructure failures only warn). Fails closed: bad exit/unparseable JSON → all_passed=False + check_error (after one reformat retry)
9. CLAUDE.md update (if `--update-claude-md`): auto-document architecture changes
10. Implementation learning: capture post-implementation insights into repo CLAUDE.md
11. PR creation: draft PR with summary, diff stats, test results
12. Review & merge (if `--auto-merge`):
    - `--review-mode single` (default): primary review + optional external reviewer (parallel, findings merged), fix agent, re-verify, push
    - `--review-mode multi`: multi-agent orchestrator with 5 parallel reviewers that fix in-session; signal line parsed (REVIEW_DONE/REVIEW_FIXED/REVIEW_FAILED)
13. CI watch + auto-fix: on CI failure, build_ci_fix prompt with accumulated prior-attempt context; re-verify locally; push; stalemate detection on consecutive no-change SHAs; CI learning saved to CLAUDE.md

Retry loop: on failure, retry up to `--max-retries` (default 3), feeding an ACCUMULATED history of prior failed attempts (last 3, 2k chars each — `_format_impl_attempt`) into the next prompt, mirroring the CI-fix loop. Budget exhaustion raises `BudgetExhaustedError` before any paid phase starts (recorded as `BUDGET_EXHAUSTED`, never retried). After exhaustion: label `auto-fix-failed`, comment reason, log to dead-letter queue (enriched with failure_category, last_phase, attempts, cost/tokens, status_detail).

## Prompts System

Templates live in `src/autocoder/prompts/*.md`. Loaded via `prompts.load(name, repo_path)`:

- **Repo overrides**: `{repo}/.autocoder/prompts/<name>.md` wins over the packaged default. Same for `{repo}/.autocoder/prompts/agents/<name>.md`.
- **Agent expansion**: `{{agent:X}}` markers are replaced with the content of `agents/X.md` (braces in expanded content are doubled so later `str.format()` calls leave them alone).
- **Caching**: `@lru_cache` on `load()`.

Templates:

- `implement.md` / `implement_with_plan.md` — coding prompts (brief prepended at call site)
- `implement_brief.md` — pre-implement orchestrator (spawns 3 advisors)
- `plan.md` — read-only analysis prompt
- `review.md` / `review_fix.md` — single-reviewer prompts
- `review_multi.md` — multi-agent review orchestrator (5 parallel reviewers, fix in-session)
- `testplan.md` / `testplan_fix.md` — acceptance criteria verification
- `prioritize.md` — issue triage (P0-P3)
- `detect_build.md` — AI-powered build command detection
- `build_fix.md` — focused build failure fix
- `verify_fix.md` — focused in-place fix for lint/unit/integration verification failures
- `ci_fix.md` — CI failure fix with accumulated prior-attempt context
- `ci_learn.md` / `impl_learn.md` — persist tribal knowledge to repo CLAUDE.md
- `update_claude_md.md` — architecture documentation update
- `agents/architecture.md`, `agents/tests.md`, `agents/risks.md` — brief advisors
- `agents/quality.md`, `agents/implementation.md`, `agents/testing.md`, `agents/simplification.md`, `agents/documentation.md` — review role briefs

## Key CLI Options

**Models**: `--model` (default `claude-sonnet-5`), `--plan-model`/`--review-model` (default `claude-opus-4-8`), `--escalation-model` (default `claude-opus-4-8`), `--triage-model`.

**Budgets**: `--token-budget`, `--daily-cap`, `--brief-budget-usd` (1.00), `--pre-verify-budget-usd` (1.50), `--review-budget-usd` (2.00).

**Retries/timeouts**: `--max-retries` (3), `--build-retries` (1), `--ci-timeout` (1800), `--stalemate-threshold` (2), `--wait-on-rate-limit` (e.g. `30s`, `5m`, `1h`; default: abort), `--idle-timeout` (SIGTERM on silent hang; default disabled), `--session-timeout` (absolute Claude subprocess cap; default disabled).

**Phases**: `--plan-mode`, `--implement-brief`/`--no-implement-brief`, `--pre-verify-critique`/`--no-pre-verify-critique`, `--verify-fix`/`--no-verify-fix` (default on), `--testplan-enforce`/`--no-testplan-enforce` (default on), `--auto-merge`, `--update-claude-md`/`--no-update-claude-md`, `--task-slice`/`--no-task-slice` (default: auto-heuristic), `--task-retries` (1), `--max-tasks` (15).

**Concurrency**: `--parallel N` (default 1) processes N issues in parallel, each in its own git worktree; `--worktree-root PATH` (default: `<repo>/.autocoder/worktrees`).

**Dashboard**: `--serve`/`--no-serve` (default off) starts a localhost SSE dashboard; `--port` (default 8765).

**Review**: `--review-mode {single,multi}`, `--external-reviewer` (preset name `codex`/`gemini`/`claude`, or full shell command — prompt piped on stdin).

**Sandbox**: `--docker`, `--update-docker`, `--docker-max-age-days` (7), `--protect-tests`, `--test-patterns`.

**Scope**: `--issue` (multi, single-issue mode), `--labels`, `--max-issues` (10), `--max-analyze` (0=unlimited), `--dry-run`.

## Key Patterns

- **Config**: single `RunConfig` passed through entire pipeline; built once in `config.build_config`
- **Per-phase sandboxes**: `build_sandbox` (implement: full write + build/test/lint; configured commands get both exact and `Bash(<cmd>:*)` prefix-wildcard entries so agents can run targeted test subsets), `build_plan_sandbox` (read-only), `build_brief_sandbox` (read-only + Task), `build_review_sandbox` (implement + Task), `build_claude_md_sandbox` (narrow write for docs), `build_detect_sandbox` (read-only)
- **Task tool**: multi-agent orchestrators (brief, multi-review) spawn parallel sub-agents via Claude Code's `Task` tool in a single assistant turn
- **Signal parsing**: multi-agent review returns `REVIEW_DONE` / `REVIEW_FIXED` / `REVIEW_FAILED: <reason>` on final line; anything else treated as ambiguous failure
- **Rate-limit handling**: `set_rate_limit_wait(seconds)` configures `invoke_agent` to sleep+retry (up to 3 times); if unset, RateLimitError propagates and stops the run
- **Stalemate detection**: `StalemateTracker` in loop.py; CI-fix and multi-review loops abort after N consecutive no-SHA-change iterations; categorized as `CI_STALEMATE`
- **External reviewer**: preset shortcuts (`codex`, `gemini`, `claude`) expand to canonical commands via `EXTERNAL_REVIEWER_PRESETS`; anything else is shlex-split; runs in parallel with primary review; findings unioned with dedup on `(file, description[:80].lower())`
- **Cost control**: per-issue `--max-budget-usd` passed to Claude CLI; daily cap stops processing; per-phase `--brief-budget-usd`/`--pre-verify-budget-usd`/`--review-budget-usd` cap orchestrator spend; `budget.issue_exhausted()` guards every paid phase — exhaustion raises `BudgetExhaustedError` (dead-lettered, never retried) instead of degrading into a $0.01 agent error
- **Prompt context injection**: implement/task prompts receive the configured build/test/lint commands (`format_commands_block`) and the FULL acceptance-criteria list extracted from the untruncated issue body (`_criteria_block`) — the 4000-char body truncation no longer hides criteria from the implementer
- **Epics**: meta/tracking/epic-labeled issues auto-expanded; sub-issues processed individually, parent closed when all succeed
- **Telemetry**: per-phase `Phase` enum (PLAN, IMPLEMENT_BRIEF, IMPLEMENT, PRE_VERIFY_CRITIQUE, REVIEW_FIX, REVIEW_MULTI, REVIEW_EXTERNAL, TESTPLAN_FIX, UPDATE_CLAUDE_MD, CI_FIX, BUILD_FIX); `FailureCategory` drives top-failure reasons
- **Priority caching**: previously triaged issues skip redundant AI prioritization (`--force-prioritize` to bypass)
- **Docker freshness**: images auto-rebuild after `--docker-max-age-days`; `--update-docker` forces `--no-cache` rebuild
- **Learning loops**: `impl_learn.md` runs after every success; `ci_learn.md` runs after each CI fix push — both write to the target repo's CLAUDE.md to persist tribal knowledge
- **Error detection**: rate-limit and auth-error pattern matching; rate-limit can wait+retry, auth-error always aborts
- **Pre-flight**: build command runs on `main` before any issue processing; aborts if main is broken
- **Logging**: `logs/` JSONL records per run; `failed_issues.jsonl` dead-letter queue

## Testing

```bash
uv run pytest
```

Tests cover: agent output parsing, build detection, config/duration/preset resolution, git operations, anti-cheat audit, verification logic, prompt loading + agent expansion + repo overrides, review parsing + merge + multi-agent signal, loop stalemate.
