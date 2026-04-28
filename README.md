# AutoCoder

Autonomous AI coding agent loop. Fetches GitHub issues, resolves them with Claude Code CLI, runs tests, reviews code, and ships PRs.

## How it works

```
Fetch issues → AI prioritize → Pre-flight build on main → Process top N
  │
  └─ Per issue (optionally in parallel worktrees):
     Branch → [Plan] → [Brief] → Implement (monolithic or task-sliced)
            → [Status check / escalate] → [Critique] → Verify → [Build fix]
            → Test plan check → CLAUDE.md update → Impl learn → Draft PR
                                                                    ↓ (fail)
                                                              Retry (3x) → Dead-letter
     With --auto-merge:
     Draft PR → Review (single, or two-stage: spec → quality, + optional external)
             → Fix → Re-verify → Push → Mark ready → Wait CI
             → Auto-fix CI (with stalemate detection + arch critique on near-stalemate)
             → CI learn → Squash merge
```

Optional phases in `[brackets]`. Pipeline is per-issue; a single issue may run many phases, each budgeted separately.

1. **Fetch + prioritize** — All open issues via `gh issue list`; Claude ranks P0→P3 by automability, complexity, dependencies, label hints. Priority results cache per-issue. Epic / meta-issue / tracking-labeled issues auto-expand into sub-issues; the parent closes when all succeed.
2. **Pre-flight build** — Runs the build command on `main` before any issue. Aborts if main is broken.
3. **Plan** *(optional, `--plan-mode`)* — Read-only analysis produces a plan fed into the implement phase.
4. **Brief** *(default on)* — Orchestrator spawns 3 parallel advisors (architecture / tests to add / risks) and synthesizes a compact design brief prepended to the implementer's prompt.
5. **Implement** — Agent writes code with scoped permissions. Two execution modes:
   - **Monolithic** (default) — single long Claude session.
   - **Task-sliced** (via `--task-slice`, or auto when an issue has ≥3 acceptance-criteria checkboxes or body >1500 chars) — orchestrator writes `.autocoder/plan-<N>.md` with a `- [ ]` task checklist, then each task runs in a fresh Claude subprocess that marks its checkbox done. Plan is lint-checked for placeholder phrases (TBD/TODO/"as appropriate") and regenerated once if vague.
6. **Status check / escalate** *(default on)* — Implementer signals `STATUS: DONE | DONE_WITH_CONCERNS | BLOCKED | NEEDS_CONTEXT`. On `BLOCKED` / `NEEDS_CONTEXT`, AutoCoder retries once in the same attempt with `--escalation-model` (a stronger model) before falling back to the normal retry loop.
7. **Anti-cheat audit** *(if `--protect-tests`)* — Reject if test files were modified.
8. **Critique** *(default on)* — Multi-agent shift-left review on the pending diff. Fixes in-session or fails the attempt before verification.
9. **Verify** — lint → unit → integration → build. Stops on first failure.
   - **Build fix** — On build failure, a focused agent investigates root cause + patterns, then fixes (separate `--build-retries` budget) before falling back to the main retry loop.
10. **Test plan check** — Parses acceptance criteria checkboxes from the issue and verifies each against the diff. Fix + re-verify if any fail.
11. **CLAUDE.md update + implementation learn** — Auto-document architecture changes and capture post-implementation insights into the target repo's CLAUDE.md.
12. **PR** — Draft PR with summary, diff stats, verify results.
13. **Review** *(with `--auto-merge`)* — Two-stage pipeline (see below) or single reviewer. Optionally in parallel: external reviewer (Codex/Gemini/custom) whose findings are merged or passed as context.
14. **CI watch + auto-fix** — Wait for CI; on failure, accumulate prior-attempt context, have the agent investigate root cause + fix, re-verify locally, push. Stalemate detection aborts after N no-change iterations; an architectural critique runs on near-stalemate to question the pattern instead of patching again. CI learnings saved to CLAUDE.md.
15. **Merge** — Squash-merge on green; otherwise the PR stays open.

Retry loop: failures feed error context back and retry up to `--max-retries`. After exhaustion: label `auto-fix-failed`, comment the reason, dead-letter.

## Install

```bash
uv sync
```

Requires [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) and [GitHub CLI](https://cli.github.com/) installed and authenticated.

## Usage

```bash
# Baseline: top 10 issues, full pipeline, draft PRs
uv run autocoder --repo /path/to/repo --test-cmd "npm test" --lint-cmd "npm run lint"

# Auto-merge with two-stage review + external second opinion
uv run autocoder --repo /path/to/repo --test-cmd "npm test" \
  --auto-merge --review-mode multi --external-reviewer codex

# Parallel: 4 issues at once, each in its own git worktree, with live dashboard
uv run autocoder --repo /path/to/repo --test-cmd "npm test" \
  --parallel 4 --serve

# Single issue, plan first, wait through rate limits, kill silent hangs
uv run autocoder --repo /path/to/repo --issue 42 \
  --plan-mode --wait-on-rate-limit 5m --idle-timeout 5m

# Cheaper run: disable brief + critique
uv run autocoder --repo /path/to/repo --test-cmd "npm test" \
  --no-implement-brief --no-pre-verify-critique

# Preview without executing
uv run autocoder --repo /path/to/repo --dry-run
```

## Options

### Models

| Flag | Default | Description |
|---|---|---|
| `--model` | `claude-sonnet-4-6` | Implementation, fix, and CI-fix tasks |
| `--plan-model` | `claude-opus-4-6` | Planning phase (requires `--plan-mode`) |
| `--review-model` | `claude-opus-4-6` | Code review (requires `--auto-merge`) |
| `--triage-model` | `haiku` | Issue prioritization |
| `--escalation-model` | `claude-opus-4-7` | Stronger model used only for in-attempt retries on `BLOCKED` / `NEEDS_CONTEXT` |
| `--effort` | `max` | `min` / `low` / `medium` / `high` / `max` |

### Scope

| Flag | Default | Description |
|---|---|---|
| `--repo` | *(required)* | Path to target git repository |
| `--labels` | *(all issues)* | Comma-separated labels to filter by |
| `--issue` | — | Specific issue number(s); repeatable; skips fetch + prioritization |
| `--max-issues` | `10` | Issues to process per run |
| `--max-analyze` | `0` (all) | Issues to fetch/analyze |
| `--dry-run` | off | Show plan without executing |

### Verification

| Flag | Default | Description |
|---|---|---|
| `--build-cmd` | *(auto)* | Build command. AI-detected from the repo if omitted. |
| `--build-retries` | `1` | Build-failure retries (separate budget from `--max-retries`) |
| `--test-cmd` | — | Unit test command (e.g. `npm test`, `pytest`) |
| `--lint-cmd` | — | Lint command |
| `--integration-cmd` | — | Integration test command |
| `--protect-tests` | off | Prevent agent from modifying test files |
| `--test-patterns` | `**/test_*,**/*_test.*,**/tests/**,**/*.test.*,**/*.spec.*` | Glob patterns for test files |

### Phase toggles

| Flag | Default | Description |
|---|---|---|
| `--plan-mode` | off | Read-only plan before each implement phase |
| `--implement-brief` / `--no-implement-brief` | on | 3-advisor design brief prepended to implementer prompt |
| `--task-slice` / `--no-task-slice` | auto | Slice implement into fresh-context tasks. Auto-enabled on ≥3 acceptance criteria or body >1500 chars. |
| `--escalate-on-block` / `--no-escalate-on-block` | on | On implementer `BLOCKED` / `NEEDS_CONTEXT`, retry once with `--escalation-model` |
| `--pre-verify-critique` / `--no-pre-verify-critique` | on | Multi-agent critique on pending diff before verification |
| `--update-claude-md` / `--no-update-claude-md` | on | Auto-update target repo's CLAUDE.md |
| `--auto-merge` | off | Review, fix, wait for CI, squash-merge |
| `--ci-arch-review` / `--no-ci-arch-review` | on | On near-stalemate CI fix, run architectural critique |

### Task-slicing

| Flag | Default | Description |
|---|---|---|
| `--task-retries` | `1` | Per-task retry count within the task-slice loop |
| `--max-tasks` | `15` | Cap on task count; plans above this fall back to monolithic implement |

### Concurrency + dashboard

| Flag | Default | Description |
|---|---|---|
| `--parallel` | `1` | Process N issues concurrently, each in its own git worktree |
| `--worktree-root` | `<repo>/.autocoder/worktrees` | Directory for per-issue git worktrees |
| `--serve` / `--no-serve` | off | Start a localhost SSE dashboard streaming live run events |
| `--port` | `8765` | Dashboard port when `--serve` is set |

### Review

| Flag | Default | Description |
|---|---|---|
| `--review-mode` | `single` | `single` (one reviewer) or `multi` (two-stage: spec compliance → 5 parallel quality reviewers) |
| `--external-reviewer` | — | Second opinion. Preset (`codex` / `gemini` / `claude`) or full shell command; prompt piped on stdin |

### Retries + resilience

| Flag | Default | Description |
|---|---|---|
| `--max-retries` | `3` | Retry attempts per issue |
| `--ci-timeout` | `1800` | Seconds to wait for CI per attempt |
| `--stalemate-threshold` | `2` | Abort review/CI-fix loops after N consecutive no-change iterations |
| `--wait-on-rate-limit` | — | On rate-limit errors, wait duration (`30s`/`5m`/`1h`) and retry up to 3 times. Default: abort. |
| `--idle-timeout` | — | Kill Claude subprocess if no stdout for this long (e.g. `5m`, `90s`). Default: disabled. |
| `--session-timeout` | — | Hard cap on Claude subprocess duration (e.g. `30m`, `1h`). Default: disabled. |

### Budgets

| Flag | Default | Description |
|---|---|---|
| `--token-budget` | `500000` | Per-issue token budget |
| `--daily-cap` | `5000000` | Daily token cap across all issues |
| `--brief-budget-usd` | `1.00` | Pre-implement brief orchestrator cap |
| `--pre-verify-budget-usd` | `1.50` | Pre-verify critique orchestrator cap |
| `--review-budget-usd` | `2.00` | Multi-agent review orchestrator cap |

### Prioritization

| Flag | Default | Description |
|---|---|---|
| `--auto-prioritize` / `--no-auto-prioritize` | on | AI-based priority scoring (P0→P3) |
| `--force-prioritize` | off | Bypass priority cache, re-run AI analysis |

### Sandbox + logs

| Flag | Default | Description |
|---|---|---|
| `--docker` | off | Run agent inside Docker sandbox |
| `--update-docker` | off | Force-rebuild image with latest Claude Code CLI |
| `--docker-max-age-days` | `7` | Auto-rebuild image after N days |
| `--log-dir` | `./logs` | JSONL logs directory |

## Two-stage review

With `--review-mode multi`, review runs as a gate-then-deepen pipeline:

1. **Spec compliance** *(1 agent, quick gate)* — verifies the diff actually fulfills the issue's acceptance criteria. If it fails, quality review is skipped and the attempt is rejected.
2. **Code quality** *(5 parallel specialists)* — quality, implementation, testing, simplification, documentation. The orchestrator consolidates findings, verifies each against source, drops low-severity and false positives, and fixes confirmed issues in-session.

Outcome is signaled on a final line (`REVIEW_DONE` / `REVIEW_FIXED` / `REVIEW_FAILED: <reason>`).

## External reviewer

`--external-reviewer` provides a second opinion alongside the primary review. Presets:

| Preset | Expands to |
|---|---|
| `codex` | `codex exec` |
| `gemini` | `gemini` |
| `claude` | `claude -p --output-format text` |

Anything else is shell-split as a full command (e.g. `"codex exec -m gpt-5"`). The review prompt is piped on stdin; the command must emit a JSON array of findings on stdout. In `single` mode, external findings are merged (dedup on file + description); in `multi` mode they're passed as additional context to the orchestrator.

## Parallel execution

`--parallel N` processes up to N issues concurrently. Each worker:

- Creates a git worktree under `--worktree-root` (default `<repo>/.autocoder/worktrees/<branch>`)
- Runs the full pipeline against its worktree (including verify, build, lint, push, CI watch)
- Cleans up the worktree on completion (success or terminal failure)

Worktrees isolate file-state per issue so the agent can edit, test, and commit without colliding with peers. Token / daily / per-phase budgets are shared across workers.

## Live dashboard

`--serve` starts a localhost SSE dashboard at `http://127.0.0.1:<port>` (default 8765). It streams per-phase events as they happen — useful when running in `--parallel` mode to see what each worker is doing.

## Implementer status signaling + escalation

The implementer is asked to end its run with one of:

- `STATUS: DONE` — finished cleanly
- `STATUS: DONE_WITH_CONCERNS` — finished but flagged concerns (proceed to verification)
- `STATUS: BLOCKED` — could not finish
- `STATUS: NEEDS_CONTEXT` — needs more information

With `--escalate-on-block` (default on), `BLOCKED` / `NEEDS_CONTEXT` triggers a single in-attempt retry using `--escalation-model` (default `claude-opus-4-7`). If that also fails, the attempt falls into the normal retry loop with the failure context.

## Watchdogs

Long-running Claude sessions can hang silently. Two timeouts catch this:

- `--idle-timeout` — kills the subprocess if no stdout arrives for the duration (e.g. `5m`)
- `--session-timeout` — hard cap on subprocess duration regardless of activity (e.g. `1h`)

Both fire `SIGTERM`; the kill is treated as an `IdleTimeoutError` and feeds back into the retry loop.

## Rate-limit handling

By default, Claude API rate-limit errors abort the run (retrying immediately won't help). With `--wait-on-rate-limit 5m`, AutoCoder sleeps and retries each agent invocation up to 3 times before giving up.

## Cost control

- **Per-issue token budget** + **daily cap** halt processing when thresholds trip
- **Per-phase USD caps** limit orchestrator spend (`--brief-budget-usd`, `--pre-verify-budget-usd`, `--review-budget-usd`); the main implement phase uses `--max-budget-usd` computed from remaining issue budget
- **Model selection** — use `--model claude-sonnet-4-6` (default) for cheaper runs; upgrade `--review-model` / `--plan-model` / `--escalation-model` independently

## Sandboxing

**Default:** Agent permissions scoped via `--allowedTools`. Each phase gets a tailored sandbox:
- **Implement**: file writes + git staging + build/test/lint commands
- **Plan / Brief / Detect-build**: read-only (no writes)
- **Multi-agent review / Brief**: `Task` tool enabled to spawn parallel sub-agents
- **CLAUDE.md update**: narrow write scope for docs only

**Docker mode (`--docker`):** Runs the agent inside a container with host Claude OAuth tokens (from macOS keychain) mounted in. Image auto-builds on first use and auto-rebuilds after `--docker-max-age-days` (default 7). `--update-docker` forces a `--no-cache` rebuild to pull the latest CLI.

## Prompt overrides

Every prompt template has a repo-level override. Drop a file at `{target-repo}/.autocoder/prompts/<name>.md` to replace the packaged default. Agent role briefs live under `agents/<name>.md` and are expanded into orchestrator prompts via `{{agent:<name>}}` markers.

Useful overrides:
- `prompts/implement.md` — project-specific coding conventions
- `prompts/agents/architecture.md` / `tests.md` / `risks.md` — shape the pre-implement brief
- `prompts/agents/quality.md` etc. — customize what reviewers look for
- `prompts/agents/spec_compliance.md` — what counts as "fulfills the spec" in stage 1 review
- `prompts/build_fix.md` / `prompts/ci_fix.md` / `prompts/ci_fix_arch.md` — debugging strategy
- `prompts/prioritize.md` — project-specific priority heuristics

## Learning loops

AutoCoder writes back into the target repo's CLAUDE.md to persist tribal knowledge:
- **`impl_learn.md`** runs after every successful implementation — captures non-obvious patterns discovered while fixing the issue
- **`ci_learn.md`** runs after each CI fix push — records what broke CI and how to avoid it

These updates are committed to the PR branch and propagate to future runs via the main implement prompt.

## Anti-cheat

With `--protect-tests`, test files are made read-only (via filesystem perms) during agent execution. After each attempt, the diff is audited — if the agent modified test files instead of fixing code, the attempt is rejected and retried with that context.

## Logs + telemetry

Each run produces a JSONL file in `--log-dir` with per-attempt records: issue number, per-phase tokens + cost + model, verify stages, review findings, test plan items, outcome, PR URL.

Run summary printed at the end includes: issues processed, success/retry/skip counts, total cost, cache hit rate, token breakdowns per phase / model / issue, and top failure categories.

Failed issues are appended to `failed_issues.jsonl` for manual triage.

## Tests

```bash
uv run pytest
```
