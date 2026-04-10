You are verifying whether a code change fulfills the acceptance criteria of a GitHub issue.

## Issue
Title: {title}
Body:
{body}

## Acceptance Criteria
{criteria_list}

## Git Diff
```
{diff}
```

For each acceptance criterion, determine if the diff addresses it.

Rules:
- "pass" means the criterion is clearly addressed by the changes in the diff
- "fail" means the criterion is NOT addressed, or only partially addressed
- Provide brief, specific evidence citing file names and what was added/changed

Respond with ONLY a JSON array. No markdown fences. No explanation.
Each entry: {{"criterion": "exact criterion text", "status": "pass"|"fail", "evidence": "Brief specific evidence"}}