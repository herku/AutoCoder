You are orchestrating a thorough code review for the change below. You will spawn 5 specialized review sub-agents IN PARALLEL, consolidate their findings, verify each one against the actual source, then fix confirmed issues in-session.

## Context

Working directory is the git repository with the proposed change already applied. Primary branch is `main`. The diff to review:

```
{diff}
```

External reviewer findings (may be empty):

{external_findings}

## Step 1 — Gather context

Use `Bash(git diff:*)`, `Bash(git log:*)`, and `Read` to understand the branch's full set of changes. Look at files touched, commit messages, and surrounding code that calls into the modified functions. Do NOT skip this — a diff in isolation is incomplete.

## Step 2 — Spawn 5 review agents in parallel

Issue **exactly 5 `Task` tool calls in a single assistant turn**. Do NOT use `run_in_background`. All 5 must be foreground so they run in parallel and block until all complete.

Each sub-agent gets:
- `subagent_type: "general-purpose"`
- `description`: the role name (e.g. "Quality review")
- `prompt`: the role brief below, suffixed with the diff and any external findings, plus `"Return a compact list of findings, each with file:line, description, severity (critical/medium/low), and why it matters. If nothing found, say 'NONE'."`

Role briefs to spawn:

### Agent: quality
{{agent:quality}}

### Agent: implementation
{{agent:implementation}}

### Agent: testing
{{agent:testing}}

### Agent: simplification
{{agent:simplification}}

### Agent: documentation
{{agent:documentation}}

## Step 3 — Consolidate and verify

After all 5 agents return:

1. Collect every finding from all agents plus any external reviewer findings.
2. Deduplicate: findings pointing at the same file+line with the same defect count once, even if worded differently.
3. Verify each finding: open the file and confirm the defect is real. Drop false positives — sub-agents can hallucinate or misunderstand context.
4. Drop severity=low. Keep critical and medium only.
5. Also include any pre-existing lint or test failures on this branch — those are worth fixing while we're here.

## Step 4 — Fix confirmed issues

For each verified finding, fix it directly using Edit/Write. After fixing:
- Run the test command if available to make sure you didn't break anything.
- If a fix is too risky or out-of-scope for this review pass, leave it and note why.

## Step 5 — Signal outcome

End your final message with EXACTLY one of these lines as the last line:

- `REVIEW_DONE` — no real issues found (after dedup and verification).
- `REVIEW_FIXED` — found issues, fixed them all.
- `REVIEW_FAILED: <short reason>` — found critical issues that could not be fixed.

No other text after the signal line.
