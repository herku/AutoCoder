You are running ROUND 1 of a two-stage code review: **spec compliance**. The goal is to verify the implementation does what the issue asked for (nothing more, nothing less) — code-quality concerns are deferred to round 2.

## Context

Working directory is the git repository with the proposed change already applied. The diff to review:

```
{diff}
```

Issue body / acceptance criteria for reference:

```
{issue_body}
```

## Step 1 — Gather context

Use `Bash(git diff:*)`, `Bash(git log:*)`, and `Read` to confirm the full set of changes and the surrounding code that calls into modified functions. The diff in isolation is not enough.

## Step 2 — Spawn the spec-compliance reviewer

Issue exactly **one** `Task` tool call (foreground, not `run_in_background`):

- `subagent_type: "general-purpose"`
- `description: "Spec-compliance review"`
- `prompt:` the role brief below, suffixed with the diff and issue body, plus `"Return a compact list of findings in the format: 'MISSING|EXTRA|MISINTERPRETED — file:line — description tied to a specific requirement'. If the implementation matches the spec, say 'NONE' on its own line."`

### Agent: spec_compliance
{{agent:spec_compliance}}

## Step 3 — Verify and fix

After the agent returns:

1. Open every cited file:line and confirm the finding is real. Drop false positives.
2. For each verified finding:
   - **MISSING** — implement the missing piece using Edit / Write. Run any test that covers the area.
   - **EXTRA** — remove the over-built code. If removal would break unrelated callers added in this same diff, also remove those.
   - **MISINTERPRETED** — reshape the implementation. Tests covering the wrong-shape behaviour should be updated to cover the right shape.
3. After fixing, re-spawn the spec-compliance agent ONCE to confirm the spec is now met. If the second pass still finds spec violations, do not loop — emit `SPEC_FAILED` with the remaining list.

## Step 4 — Signal outcome

End your final message with EXACTLY one of these lines as the last line:

- `SPEC_DONE` — spec was already met on the first pass; no edits needed.
- `SPEC_FIXED` — spec was off; you fixed the gaps and the second pass confirmed compliance.
- `SPEC_FAILED: <short reason>` — spec violations remain after one fix attempt; the work is not complete.

No other text after the signal line.
