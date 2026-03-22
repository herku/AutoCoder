# AutoCoder

Autonomous AI coding agent loop. Fetches GitHub issues, resolves them with Claude Code CLI, runs tests, and ships draft PRs.

## How it works

```
Issue fetch → Branch → Claude Code agent → Lint/Test → Draft PR
                                              ↓ (fail)
                                         Retry (up to 3x) → Dead-letter
```

1. **Issue selection** — Fetches open issues by priority labels (P0→P3) via `gh issue list`
2. **Branch creation** — Creates `ai/issue-{num}` from main
3. **Agent execution** — Runs `claude -p` in headless mode with scoped permissions
4. **Verification** — Progressive pipeline: lint → unit tests → integration tests
5. **PR creation** — Opens a draft PR linked to the issue
6. **Retry or skip** — Feeds failure context back to the agent. After 3 failures, labels the issue `auto-fix-failed` and logs to a dead-letter queue

## Install

```bash
uv sync
```

Requires [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) and [GitHub CLI](https://cli.github.com/) installed and authenticated.

## Usage

```bash
uv run autocoder --repo /path/to/repo --labels P0,P1 --test-cmd "npm test" --lint-cmd "npm run lint"
```

Preview without executing:

```bash
uv run autocoder --repo /path/to/repo --labels P0,P1 --dry-run
```

## Options

| Flag | Default | Description |
|---|---|---|
| `--repo` | *(required)* | Path to target git repository |
| `--labels` | `P0,P1,P2` | Comma-separated priority labels |
| `--test-cmd` | — | Test command (e.g. `npm test`, `pytest`) |
| `--lint-cmd` | — | Lint command (e.g. `npm run lint`) |
| `--integration-cmd` | — | Integration test command |
| `--model` | `sonnet` | Claude model for coding |
| `--triage-model` | `haiku` | Model for issue summarization |
| `--max-issues` | `10` | Issues to process per run |
| `--max-retries` | `3` | Retry attempts per issue |
| `--token-budget` | `500000` | Token budget per issue |
| `--daily-cap` | `5000000` | Daily token cap |
| `--docker` | off | Run agent inside Docker sandbox |
| `--protect-tests` | off | Prevent agent from modifying test files |
| `--log-dir` | `./logs` | Directory for JSONL logs |
| `--dry-run` | off | Show plan without executing |

## Cost control

- **Model routing** — Haiku for triage/summarization, Sonnet for coding
- **Per-issue budget** — Enforced via `--max-budget-usd` on Claude CLI
- **Daily cap** — Stops processing when token limit is reached

## Sandboxing

**Default:** Agent permissions scoped via `--allowedTools` — only file operations, git, and your specified test/lint commands.

**Docker mode (`--docker`):** Runs the agent inside a container with `--network=none`. Build the image first:

```bash
docker build -t autocoder-sandbox .
```

## Anti-cheat

With `--protect-tests`, test files are made read-only during agent execution. After each attempt, the diff is audited — if the agent modified test files instead of fixing code, the attempt is rejected.

## Logs

Each run produces a JSONL file in `--log-dir` with per-attempt records: issue number, model, tokens, cost, test results, outcome, and PR URL.

Failed issues are appended to `failed_issues.jsonl` for manual triage.

## Tests

```bash
uv run pytest
```
