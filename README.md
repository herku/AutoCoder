# AutoCoder

Autonomous AI coding agent loop. Fetches GitHub issues, resolves them with Claude Code CLI, runs tests, reviews code, and ships PRs.

## How it works

```
Fetch all issues → AI auto-prioritize → Process top N
  │
  └─ Per issue:
     Branch (feat/42-fix-widget) → Claude Code agent → Lint/Test → Draft PR
                                                          ↓ (fail)
                                                     Retry (up to 3x) → Dead-letter
     With --auto-merge:
     Draft PR → Code review (claude -p) → Fix issues → Re-verify → Squash merge
```

1. **Issue selection** — Fetches all open issues via `gh issue list`, optionally filtered by `--labels`
2. **Auto-prioritize** — Sends all issues to Claude for AI-based priority scoring (P0→P3) by automability, complexity, and dependencies
3. **Branch creation** — Creates `feat/{num}-{slug}` from main
4. **Agent execution** — Runs `claude -p` with Opus and max effort, scoped permissions
5. **Verification** — Progressive pipeline: lint → unit tests → integration tests
6. **PR creation** — Opens a draft PR linked to the issue
7. **Review & merge** — With `--auto-merge`: reviews the diff, fixes critical/medium issues, re-verifies, squash-merges
8. **Retry or skip** — Feeds failure context back to the agent. After 3 failures, labels the issue `auto-fix-failed` and logs to a dead-letter queue

## Install

```bash
uv sync
```

Requires [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) and [GitHub CLI](https://cli.github.com/) installed and authenticated.

## Usage

```bash
# Process top 10 issues with auto-prioritization
uv run autocoder --repo /path/to/repo --test-cmd "npm test" --lint-cmd "npm run lint"

# Process and auto-merge
uv run autocoder --repo /path/to/repo --test-cmd "npm test" --auto-merge

# Filter by labels
uv run autocoder --repo /path/to/repo --labels "bug,priority:p0"

# Preview without executing
uv run autocoder --repo /path/to/repo --dry-run

# Single issue test run
uv run autocoder --repo /path/to/repo --max-issues 1
```

## Options

| Flag | Default | Description |
|---|---|---|
| `--repo` | *(required)* | Path to target git repository |
| `--labels` | *(all issues)* | Comma-separated labels to filter by |
| `--test-cmd` | — | Test command (e.g. `npm test`, `pytest`) |
| `--lint-cmd` | — | Lint command (e.g. `npm run lint`) |
| `--integration-cmd` | — | Integration test command |
| `--model` | `claude-opus-4-6` | Claude model for coding |
| `--effort` | `max` | Claude effort level (`min`/`low`/`medium`/`high`/`max`) |
| `--triage-model` | `haiku` | Model for issue prioritization |
| `--max-issues` | `10` | Issues to process per run |
| `--max-analyze` | `0` (all) | Issues to fetch/analyze (0 = unlimited) |
| `--max-retries` | `3` | Retry attempts per issue |
| `--token-budget` | `500000` | Token budget per issue |
| `--daily-cap` | `5000000` | Daily token cap |
| `--auto-prioritize` | on | AI-based issue prioritization (disable with `--no-auto-prioritize`) |
| `--auto-merge` | off | Review, fix, and squash-merge PRs after creation |
| `--docker` | off | Run agent inside Docker sandbox |
| `--update-docker` | off | Force-rebuild Docker image with latest Claude Code |
| `--docker-max-age-days` | `7` | Auto-rebuild Docker image if older than N days |
| `--protect-tests` | off | Prevent agent from modifying test files |
| `--log-dir` | `./logs` | Directory for JSONL logs |
| `--dry-run` | off | Show plan without executing |

## Auto-prioritize

Enabled by default. All fetched issues are sent to Claude for AI-based priority scoring before processing. Issues are ranked P0→P3 based on:

- **Automability** — Can an AI agent solve this autonomously?
- **Complexity** — How many lines of code are needed?
- **Dependencies** — Does it depend on other issues?
- **Existing labels** — Priority labels are treated as strong hints

Only the top `--max-issues` are processed after prioritization. Use `--dry-run` to preview priorities without executing.

## Auto-merge

With `--auto-merge`, after creating a draft PR:

1. **Review** — Claude reviews the full diff for critical/medium issues (bugs, security, data loss)
2. **Fix** — If issues found, the agent fixes them automatically
3. **Re-verify** — Tests run again after fixes
4. **Merge** — PR is marked ready and squash-merged

If the fix breaks tests, the original PR is merged as-is. If merge fails (e.g. branch protection), the PR stays open for human review.

## Cost control

- **Per-issue budget** — Enforced via `--max-budget-usd` on Claude CLI
- **Daily cap** — Stops processing when token limit is reached
- **Model selection** — Use `--model claude-sonnet-4-6` for cheaper runs

## Sandboxing

**Default:** Agent permissions scoped via `--allowedTools` — only file operations, git, and your specified test/lint commands.

**Docker mode (`--docker`):** Runs the agent inside a container. Requires `ANTHROPIC_API_KEY` or OAuth tokens extracted from macOS keychain. The image auto-builds on first use.

**Keeping the image fresh:** The Docker image bakes in Claude Code CLI at build time. By default, images older than 7 days are automatically rebuilt (configurable via `--docker-max-age-days`). Use `--update-docker` to force an immediate rebuild. All rebuilds use `--no-cache` to guarantee the latest CLI version.

## Anti-cheat

With `--protect-tests`, test files are made read-only during agent execution. After each attempt, the diff is audited — if the agent modified test files instead of fixing code, the attempt is rejected.

## Logs

Each run produces a JSONL file in `--log-dir` with per-attempt records: issue number, model, tokens, cost, test results, outcome, and PR URL.

Failed issues are appended to `failed_issues.jsonl` for manual triage.

## Tests

```bash
uv run pytest
```
