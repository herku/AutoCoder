{verb} GitHub issue #{issue_number}: {issue_title}

Issue body:
{body}
{acceptance_criteria_block}{commands_block}{discussion_block}{images_block}
--- IMPLEMENTATION PLAN ---
Follow this plan that was created after analyzing the codebase:

{plan_text}

--- END PLAN ---

Instructions:
- Implement the plan above step by step
- Read relevant source files before making changes
- Write or update tests that verify your changes
- Do NOT modify existing test assertions unless the issue specifically requires it
- Do NOT delete or comment out existing tests
- Run the build command after making changes to verify they compile
- Run the test suite to verify your changes work
- Keep changes minimal and focused on the issue{error_context_block}

## Before reporting STATUS: DONE

NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE.

Before you write `STATUS: DONE`, you MUST:

1. Run the project's lint command (if configured) and quote pass/fail counts.
2. Run the project's test command and quote pass/fail counts.
3. Run the project's build command (if configured) and quote exit status.
4. If any of the above fail and you cannot fix them in this session, report `STATUS: DONE_WITH_CONCERNS` (or `BLOCKED`) — do NOT report `STATUS: DONE`.

Skipping a verification step and claiming DONE is a failure mode. Evidence first, claim second.

## Report Format (REQUIRED — last line of your reply)

End your reply with EXACTLY one line of the form:

    STATUS: DONE: <one-line detail>

where the token after `STATUS:` is exactly one of: DONE, DONE_WITH_CONCERNS, BLOCKED, NEEDS_CONTEXT (no angle brackets, no other decoration).

Use:
- **DONE** — implementation complete, all verification passed (with quoted output above).
- **DONE_WITH_CONCERNS** — implementation complete but you have doubts (e.g., a test is flaky, a config is unclear, a refactor is needed). State the concern in the detail.
- **BLOCKED** — you cannot complete this task. Explain what blocked you (architectural ambiguity, missing context, unclear requirement). The orchestrator may retry with a stronger model.
- **NEEDS_CONTEXT** — you need information that wasn't provided (a file you couldn't find, an external API contract, etc.). State exactly what.

Never silently produce work you're unsure about. Bad work is worse than no work.