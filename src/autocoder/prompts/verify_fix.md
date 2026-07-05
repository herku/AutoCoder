The {stage} verification step failed. Use **systematic debugging** to fix the root cause — not the symptom. The implementation itself is believed sound; your job is to make this ONE verification stage pass without discarding that work.

## Iron Law

NO FIX WITHOUT ROOT-CAUSE INVESTIGATION FIRST. Quick patches that mask the underlying defect cause cascading bugs.

## Failing stage: {stage}

Verification command: `{verify_cmd}`

## Verification output

```
{verify_output}
```

## Phase 1 — Root-cause investigation

1. **Read the output completely.** Test and lint failures usually name the exact file, line, and expectation. Don't skim.
2. **Identify the first failure.** Failures often cascade — fix the first one and the rest may disappear.
3. **Check what changed.** `git diff` and `git log -n 5 --stat` for the touched files. Which change introduced this failure? Be specific.
4. **Decide: is the code wrong, or is the expectation wrong?** For a test failure, read the test AND the implementation. The implementation on this branch is new — the failure is almost always in the new code, not the pre-existing test.

## Phase 2 — Hypothesis

State ONE hypothesis: "X fails because Y." Make the SMALLEST possible change. One variable at a time.

## Phase 3 — Implementation

1. **Fix the root cause** identified in Phase 1. ONE change.
2. **Re-run the verification command** above (a targeted subset first is fine, but finish with the full command). Quote the exit code and pass/fail counts. If new failures appear, return to Phase 1 — do not pile fixes.
3. **No "while I'm here" cleanups.** If you spotted other issues, leave them.

## Red flags — STOP

- Deleting, skipping, or commenting out a failing test
- Weakening an assertion so it passes ("assertEqual → assertIn")
- Adding lint-suppression comments (`# noqa`, `// eslint-disable`) instead of fixing the finding
- "Wrap it in try/catch and swallow the error"
- Bundling unrelated cleanups into the fix

If any of these describe what you're about to do: return to Phase 1.

## Constraints

- Fix ONLY the failing {stage} verification. Do not refactor unrelated code.
- Keep changes minimal.
- Do NOT modify existing test assertions unless they are objectively wrong for the issue being implemented.
- Do NOT delete or comment out existing tests.
