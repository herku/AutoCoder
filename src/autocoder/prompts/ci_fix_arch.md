CI has failed and prior fix attempts have not converged. Per **systematic debugging Phase 4.5**: when 3+ fixes fail in succession (or each new fix produces a new symptom in a different place), the problem is **architectural**, not a coding bug. Stop patching. Question the pattern.

You are running an **analysis-only** pass — you must NOT attempt another fix. Your job is to give the orchestrator a clear architectural recommendation so the next concrete fix attempt (if any) is informed.

## Latest CI output

```
{ci_output}
```

## Prior attempt history (all failed to resolve)

{previous_attempts}

## Your job

1. **Diagnose the pattern.** For each prior attempt, summarize: what was tried, what new symptom (if any) appeared. Look for recurrences: same root cause moving around the codebase, fixes requiring "massive refactoring" each time, fixes that create new failures elsewhere.
2. **Identify the architectural smell.** Is the abstraction wrong? Is shared state being passed around implicitly? Is there a missing seam? Is the test surface coupled to internals that keep changing? Be specific.
3. **Recommend a path forward.** Choose ONE:
   - **Refactor**: name the smallest viable architectural change that would make the bug class impossible. Estimate the scope (files, lines).
   - **Escalate**: state why this needs human intervention (missing context, business decision, multi-PR refactor out of scope).
   - **Continue with a specific patch**: if and only if you've identified a root cause prior attempts missed, state it and give the precise minimal fix. (This requires a reason prior attempts missed it — "try this" is not enough.)

## Output format

```
## Pattern
- Attempt 1: <what was tried> → <new symptom>
- Attempt 2: ...
- Attempt N: ...

## Architectural smell
<one paragraph naming the underlying issue>

## Recommendation
<one of: REFACTOR | ESCALATE | PATCH>: <one-paragraph rationale and concrete next step>
```

## Constraints

- Do NOT edit any source files. This is analysis only.
- Do NOT run tests or builds. Read the CI output and the codebase only.
- If you find yourself reaching for Edit/Write, stop — that violates the contract.
