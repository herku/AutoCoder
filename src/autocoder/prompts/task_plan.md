You are decomposing GitHub issue #{issue_number} into a sequence of small, focused tasks. Each task will be executed by a fresh Claude subprocess with no prior context, so the plan must be self-contained and the tasks must be independently actionable.

Issue: {issue_title}

Issue body:
{body}
{brief_block}
## Your job

Write a plan file to the exact path:

    {plan_path}

Use the `Write` tool. Overwrite if it already exists. Output only the file — no explanatory text in your reply.

## Plan file format (follow exactly)

```
# Task Plan: #{issue_number} — {issue_title}

## Context
<2-3 sentences: what area of the codebase, what approach>

## Tasks
- [ ] Task 1: <imperative, specific, small>
- [ ] Task 2: <imperative, specific, small>
- [ ] Task 3: ...

## Validation
- <validation command 1, e.g. npm test>
- <validation command 2, e.g. npm run lint>
```

## Task rules
- Produce between 3 and 10 tasks.
- Each task must be small enough to complete in one short fresh Claude session (< ~10 minutes).
- Each task must produce a visible change (edited file, new file, new test).
- Order tasks so earlier tasks do NOT depend on later ones.
- Do NOT add "run tests" or "verify" tasks — verification runs automatically after all tasks are done.
- Group trivial related edits into a single task; do not emit one task per line.
- Prefer tasks scoped to a single file or a tight cluster of related files.
- The last task should cover any necessary doc / comment updates.

Do not include any other sections. Do not add commentary outside the plan file.
