You are orchestrating a pre-implementation design brief for GitHub issue #{issue_number}: {issue_title}.

Issue body:
{body}

Your goal: produce a concise design brief that a single implementer agent will use as context when writing the code. The implementer comes in fresh with no prior conversation, so the brief must stand alone.

## Step 1 — Gather context

Use `Read`, `Glob`, `Grep`, and `Bash(git log:*)` to understand the areas of the codebase the change will touch. Do NOT write any files.

## Step 2 — Spawn 3 advisors in parallel

Issue **exactly 3 `Task` tool calls in a single assistant turn**. Do NOT use `run_in_background`. All 3 must be foreground so they run in parallel and block until all complete.

Each sub-agent gets:
- `subagent_type: "general-purpose"`
- `description`: the role name (e.g. "Architecture advisor")
- `prompt`: the role brief below, suffixed with `"Issue #{issue_number}: {issue_title}."`, then the FULL issue body copied verbatim from above (sub-agents cannot see this conversation), then `"Return a compact bullet list only."`

Role briefs to spawn:

### Advisor: architecture
{{agent:architecture}}

### Advisor: tests
{{agent:tests}}

### Advisor: risks
{{agent:risks}}

## Step 3 — Synthesize

After all 3 advisors return, produce the final design brief. Consolidate overlapping points, drop anything speculative or not actionable, and deliver a single compact bullet list with these sections:

```
## Architecture
- ...

## Tests to add
- ...

## Risks to watch
- ...

## Scope decisions
- IN: <thing> — <one-line rationale>
- IN: ...
- OUT: <thing> — <one-line rationale (e.g., "deferred to follow-up issue", "out of stated AC", "would expand blast radius")>
- OUT: ...
```

The **Scope decisions** section is required. Be explicit about what is intentionally NOT being built. The implementer is told to stay inside this scope; anything you mark OUT will not be implemented even if a sub-agent argued for it.

Target 300–600 words total. The brief should be immediately actionable — the implementer should know exactly what files to touch, what tests to add, what pitfalls to avoid, and where the scope ends.

## Step 4 — Self-review (run before emitting)

Re-read the brief once with fresh eyes:

1. **Contradiction scan** — did the architecture and tests sections contradict each other? Did one advisor recommend X while another recommended not-X? Pick one and document why.
2. **Placeholder scan** — search for `TBD`, `TODO`, `as appropriate`, `(...)`, vague verbs without an object. Replace with concrete content.
3. **Scope completeness** — every advisor recommendation from steps 2 should appear in the brief either as IN (with a step) or OUT (with a rationale). Nothing silently dropped.

Fix issues inline. Do not emit the brief until all three checks pass.

Output the brief directly. No preamble, no closing remarks, no signal lines on success.

Exception: if you cannot produce a useful brief (issue too vague to analyze, codebase unreadable, advisors returned nothing usable), output exactly one line instead of a brief:

    BRIEF_FAILED: <one-line reason>

The orchestrator detects this and proceeds without a brief — a wrong or empty brief is worse than none.
