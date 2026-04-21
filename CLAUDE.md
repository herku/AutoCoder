# AutoCoder

Autonomous AI coding agent that resolves GitHub issues end-to-end: fetch, prioritize, branch, implement, verify, PR, review, merge.

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
config.py       RunConfig dataclass built once at startup; Docker image age checks
loop.py         Main orchestration: fetch → prioritize → process issues
agent.py        Invokes Claude Code CLI subprocess, parses JSON, detects rate-limit/auth errors
build.py        AI-powered build command detection (claude -p) with heuristic fallback
sandbox.py      SandboxConfig: scoped allowed tools for Claude agent
issues.py       GitHub CLI wrapper: fetch/parse issues, extract acceptance criteria, priority caching
review.py       Code review phase on PR diffs
testplan.py     Acceptance criteria verification against diff
verify.py       Runs lint/unit/integration tests (stops on first failure)
budget.py       Token & cost tracking (per-issue + daily cap)
git.py          GitOps class: branching, commit, diff, rollback, merge, lockfile
anticheat.py    Protect-tests mode: read-only test files, audit violations
logger.py       JSONL logging + dead-letter queue for failures
telemetry.py    Per-phase cost/token tracking and failure categorization
epic.py         Epic/meta-issue support: process sub-issues, track progress, close parent
types.py        Dataclasses: Issue, AgentResult, VerifyResult, RunConfig, EpicResult, exceptions
prompts/        Markdown templates loaded via prompts.load(name)
```

## Pipeline Phases (per issue)

1. Branch creation: `feat/{num}-{slug}` from main
2. Plan phase (optional `--plan-mode`): read-only analysis
3. Implement phase: agent writes code with scoped permissions
4. Anti-cheat audit (if `--protect-tests`): reject if test files modified
5. Verification: lint -> unit -> integration -> build (stop on first failure)
5a. Build fix (if build fails): focused agent fixes build errors, then re-verifies
6. Test plan verification: check acceptance criteria checkboxes against diff
7. CLAUDE.md update (if `--update-claude-md`): auto-document architecture changes
8. PR creation: draft PR with summary, diff stats, test results
9. Review & merge (if `--auto-merge`): review -> fix -> re-verify -> squash-merge

Retry loop: on failure, feed error context back, retry up to `--max-retries` (default 3). After exhaustion: label `auto-fix-failed`, comment reason, log to dead-letter queue.

## Prompts System

Templates live in `src/autocoder/prompts/*.md`, loaded via `prompts.load(name)`. Each template uses `str.format()` placeholders:

- `implement.md` / `implement_with_plan.md` — coding prompts
- `plan.md` — read-only analysis prompt
- `review.md` / `review_fix.md` — code review prompts
- `testplan.md` / `testplan_fix.md` — acceptance criteria verification
- `prioritize.md` — issue triage (P0-P3)
- `detect_build.md` — AI-powered build command detection
- `build_fix.md` — focused build failure fix
- `update_claude_md.md` — architecture documentation update

## Key CLI Options

`--model` (default `claude-sonnet-4-6`), `--plan-model`, `--review-model`, `--implement-model` for per-phase model selection. `--effort`, `--max-issues`, `--token-budget`, `--daily-cap`, `--auto-merge`, `--docker`, `--update-docker`, `--docker-max-age-days` (default 7), `--protect-tests`, `--dry-run`, `--issue` (single issue mode), `--update-claude-md`.

## Key Patterns

- **Config**: single `RunConfig` passed through entire pipeline
- **Sandbox**: `SandboxConfig` restricts Claude's allowed tools (Read/Edit/Write/Glob/Grep always; git/test/lint optional)
- **Cost control**: per-issue `--max-budget-usd` via Claude CLI, daily cap stops processing
- **Epics**: meta-issues with sub-issue lists are auto-expanded; sub-issues processed individually, parent closed when all succeed
- **Telemetry**: per-phase cost/token tracking, failure categorization (lint/test/review/rate-limit/etc.)
- **Priority caching**: previously triaged issues skip redundant AI prioritization
- **Docker freshness**: images auto-rebuild after `--docker-max-age-days` (default 7); `--update-docker` forces immediate rebuild with `--no-cache`
- **Error detection**: rate-limit and auth-error pattern matching aborts immediately
- **Logging**: `logs/` directory, JSONL records per run, `failed_issues.jsonl` dead-letter queue

## Testing

```bash
uv run pytest
```

Tests cover: agent output parsing, git operations, anti-cheat audit, verification logic.
