{verb} a single task for GitHub issue #{issue_number}: {issue_title}.

You are in a fresh Claude session with no prior context.

A task plan exists at: {plan_path}

## Your task (execute EXACTLY this one)

{task_text}

## Rules
- Read {plan_path} first to understand the overall plan and what has already been completed.
- Read any relevant source files before making changes.
- Make the minimal code change required for THIS task only.
- Do NOT execute any other task in the plan — even if it seems trivial and related.
- Do NOT modify existing test assertions unless this task explicitly requires it.
- Do NOT delete or comment out existing tests.
- After finishing the change:
  1. Update {plan_path} to mark THIS task's checkbox as done by changing its `- [ ]` to `- [x]`. Leave all other tasks untouched.
  2. Do NOT commit anything. Leave all changes staged.
- Do NOT run the full test suite. A single targeted test for your change is fine.
- If the task is unclear, already done, or blocked: STOP, briefly explain why, and leave the checkbox as `- [ ]`.

## Before reporting STATUS: DONE for THIS task

NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE.

A single targeted test for THIS task's change is sufficient (do not run the full suite). Quote its pass/fail in your report. If the test does not exist yet for this task, run a focused build/lint check on the touched files and quote that.

## Report Format (REQUIRED — last line of your reply)

End your reply with EXACTLY one line of the form:

    STATUS: DONE: <one-line detail>

where the token after `STATUS:` is exactly one of: DONE, DONE_WITH_CONCERNS, BLOCKED, NEEDS_CONTEXT (no angle brackets, no other decoration).

Use:
- **DONE** — task done, checkbox flipped to `- [x]`, targeted verification passed.
- **DONE_WITH_CONCERNS** — task done but a follow-up may be needed; state the concern.
- **BLOCKED** — cannot complete this task (architectural ambiguity, dependency missing, contradicting earlier task). Leave the checkbox as `- [ ]`. The orchestrator may retry with a stronger model.
- **NEEDS_CONTEXT** — you need information not provided in the plan or issue body. State what.

Issue body (for context):
{body}
{acceptance_criteria_block}{commands_block}{discussion_block}{error_context_block}
