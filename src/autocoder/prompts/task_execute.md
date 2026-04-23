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

Issue body (for context):
{body}
{error_context_block}
