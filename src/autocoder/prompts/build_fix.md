The build failed. Use **systematic debugging** to fix the root cause — not the symptom.

## Iron Law

NO FIX WITHOUT ROOT-CAUSE INVESTIGATION FIRST. Quick patches that mask the underlying defect cause cascading bugs.

## Build output

```
{build_output}
```

Build command: `{build_cmd}`

## Phase 1 — Root-cause investigation

1. **Read the build output completely.** Read every error and warning. Compiler / type-checker errors usually contain the exact problem (file, line, expected vs. found). Don't skim.
2. **Identify the first error.** A build often cascades — fix the first compilation error and the rest may disappear. Don't start with the last error.
3. **Check what changed.** `git log -n 5 --stat` and `git diff` for the touched files. Which change introduced this build break? Be specific.
4. **Trace types/imports/symbols.** If the error is "undefined symbol X" or "type mismatch on Y", find where X / Y is declared. Did this branch rename, move, or change the signature? Fix at the declaration site, not at every call site.

## Phase 2 — Pattern analysis

1. Find a similar working construct in the codebase: a sibling type, a sibling import, a similar function call.
2. Diff working vs. broken: type signatures, import paths, generic params, visibility modifiers. List every difference.
3. If using a library API: read the library's actual current signature (don't guess). The error message usually quotes it.

## Phase 3 — Hypothesis

State ONE hypothesis: "X fails because Y." Make the SMALLEST possible change. One variable at a time.

## Phase 4 — Implementation

1. **Fix the root cause** identified in Phase 1. ONE change.
2. **Re-run the build command** above. Quote the exit code and any remaining errors. If new errors appear, return to Phase 1 — do not pile fixes.
3. **No "while I'm here" cleanups.** If you spotted other issues, leave them.

## Red flags — STOP

- "Cast it to `any` / `Object` / `void*` to make the type checker shut up"
- "Add `// @ts-ignore` / `# type: ignore` and move on"
- "Comment out the broken import"
- "Wrap it in try/catch and swallow the error"
- Bundling unrelated cleanups into the build fix
- Fixing the LAST error first instead of the first

If any of these describe what you're about to do: return to Phase 1.

## Constraints

- Fix ONLY the build failure. Do not refactor unrelated code.
- Keep changes minimal.
- Do NOT modify test assertions.
