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

## Goal
<one sentence — what user-visible behaviour ships when this plan is done>

## Architecture
<2-3 sentences — which area of the codebase, the chosen approach, and any assumptions>

## Files touched
- `path/to/foo.py` — <one-line responsibility>
- `path/to/bar_test.py` — <one-line responsibility>
<list every file the plan creates or modifies>

## Tasks

### Task 1: <imperative title>
**Files:**
- Modify: `exact/path/to/file.py:42-58`
- Test: `tests/exact/path/test_x.py`

- [ ] Step 1: Write the failing test.
  ```python
  def test_widget_handles_empty_input():
      assert Widget("").render() == ""
  ```
- [ ] Step 2: Run test, confirm it fails.
  ```bash
  pytest tests/exact/path/test_x.py::test_widget_handles_empty_input -v
  ```
  Expected: FAIL with `AttributeError: 'Widget' object has no attribute 'render'`.
- [ ] Step 3: Implement the minimal change.
  ```python
  def render(self) -> str:
      return self._value
  ```
- [ ] Step 4: Run test, confirm it passes.
  ```bash
  pytest tests/exact/path/test_x.py::test_widget_handles_empty_input -v
  ```
  Expected: PASS.

### Task 2: ...
(same structure)

## Validation
- <one-line full validation command, e.g. `npm test && npm run lint`>
```

## Task rules

- Produce between 1 and 10 tasks. Group trivial related edits into a single task.
- Each task must be small enough to complete in one short fresh Claude session (≈ 2–10 minutes).
- Each task **must** include the `Files:` block (Create / Modify / Test) and at least one `- [ ]` step.
- Each code-changing step must include a fenced code block showing the exact code (or a minimal-diff hunk) to write.
- Each test step must include the exact run command and the expected pass/fail line.
- Order tasks so earlier tasks do NOT depend on later ones.
- Do NOT add a task whose only work is "run the full test suite" or "verify" — verification runs automatically after all tasks.
- The last task should cover any necessary doc / comment updates.

## No placeholders (REQUIRED)

The following phrases are FORBIDDEN anywhere in the plan. They are signs of an incomplete plan and will cause this generation to be rejected and re-run:

- `TBD`, `TODO`, `FIXME`
- `implement later`, `fill in details`, `as appropriate`
- `add appropriate error handling`, `add validation`, `handle edge cases` (without showing how)
- `Similar to Task N`, `same as above`, `see Task N` (repeat the actual content)
- `(...)`, `<elided>`, `<omitted>`
- References to types, functions, methods, or files that are not defined elsewhere in the plan or in the existing codebase

If you find yourself writing one of these, STOP and write the actual content the executor will need.

## Self-review (run before emitting)

After drafting, re-read the plan with fresh eyes and answer:

1. **Spec coverage** — for each acceptance criterion in the issue body, name the task that implements it. Any criterion without a task is a gap; add a task.
2. **Placeholder scan** — search for the forbidden phrases above. Replace each with the real content.
3. **Type / name consistency** — every type, function, or method named in a later task must match how it was defined in an earlier task or in the existing codebase. A `clearLayers()` in Task 3 and `clearFullLayers()` in Task 7 is a bug.

Fix issues inline. Do not emit the plan until the three checks all pass.

## Output

Do not include any sections beyond those listed above. Do not add commentary outside the plan file.
