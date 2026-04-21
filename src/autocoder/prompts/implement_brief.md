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
- `prompt`: the role brief below, suffixed with `"Issue #{issue_number}: {issue_title}. Body: <same body as above>. Return a compact bullet list only."`

Role briefs to spawn:

### Advisor: architecture
{{agent:architecture}}

### Advisor: tests
{{agent:tests}}

### Advisor: risks
{{agent:risks}}

## Step 3 — Synthesize

After all 3 advisors return, produce the final design brief. Consolidate overlapping points, drop anything speculative or not actionable, and deliver a single compact bullet list with three sections:

```
## Architecture
- ...

## Tests to add
- ...

## Risks to watch
- ...
```

Target 300–600 words total. The brief should be immediately actionable — the implementer should know exactly what files to touch, what tests to add, and what pitfalls to avoid.

Output the brief directly. No preamble, no closing remarks. No signal lines.
