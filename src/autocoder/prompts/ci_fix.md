CI checks failed on the pull request. Use **systematic debugging** to fix the root cause — not the symptom.

## Iron Law

NO FIX WITHOUT ROOT-CAUSE INVESTIGATION FIRST. Symptom patches that mask the underlying bug are a failure mode, not a quick win.

If you find yourself thinking "let me just try changing X and see if it works", STOP and return to Phase 1.

## CI output

```
{ci_output}
```

## Phase 1 — Root-cause investigation

Before proposing any fix:

1. **Read the CI output completely.** Read the full failing step, the full assertion / error message, the full stack trace. Note exact file paths, line numbers, error codes.
2. **Identify the failing layer.** A CI run is a chain: workflow → install → build → lint → test → assertion. Where exactly did it break? Often the visible failure is downstream of the real one — scroll up.
3. **Check what changed.** `git log -n 5 --stat` and `git diff main...HEAD -- <relevant files>`. Which change in this branch could plausibly cause this failure? Be specific about the file and line.
4. **Trace data flow.** Where did the bad value originate? Trace backward through the call chain until you reach the source. Don't fix where the error appears — fix where the bad value started.
5. **For multi-component failures:** if the chain has multiple boundaries (e.g., env var → CI step → build script → test), instrument each boundary first to find which one breaks. Add diagnostic logging, push, and gather evidence — THEN fix.

## Phase 2 — Pattern analysis

Find a working example to compare against:

1. Locate a passing test in the same suite or a similar feature elsewhere in the codebase.
2. Diff working vs. broken: every difference is a candidate, however small. Don't dismiss differences as "that can't matter".
3. Check dependencies: what does the working code rely on (config, env, fixtures, prior setup) that the broken code might not?

## Phase 3 — Hypothesis

State ONE hypothesis explicitly: "X fails because Y." Then make the SMALLEST possible change to test it. One variable at a time. Do not bundle "while I'm here" cleanups.

If you genuinely don't know, say so and add diagnostic logging instead of guessing.

## Phase 4 — Implementation

1. **Write a failing test** that captures the bug at the appropriate layer (unit, integration, or both). The test must fail for the same reason CI fails.
2. **Fix the root cause** identified in Phase 1. ONE change. No bundled refactoring.
3. **Verify locally**: run the same lint / test / build commands CI runs. Quote the output. The previously-failing test must pass.
4. **No "while I'm here" changes.** If you spotted other issues, leave them.

## Red flags — STOP if any apply

- "Quick fix for now, investigate later"
- "Just try changing X and see if it works"
- "Probably X — let me fix that"
- "I don't fully understand but this might work"
- "Tests are flaky — let me retry / increase timeout"
- Proposing a fix without naming the root cause
- Bundling unrelated changes into the fix commit

If any of these describe what you're about to do: return to Phase 1.

## Constraints

- Fix ONLY the issues causing CI to fail. Do not refactor unrelated code.
- Keep changes minimal.
- Do NOT modify existing test assertions unless the original test was wrong (and explain why in the commit message).
- Do NOT delete or comment out tests.
