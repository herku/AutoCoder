You just implemented changes for a GitHub issue. Review what happened and capture any learnings in CLAUDE.md.

## Changes made (diff stats)
{diff_stats}

## Verification results
{verify_summary}

## Instructions
- Read the current CLAUDE.md
- If a `## Implementation Notes` section exists, merge new findings into it. Otherwise append one.
- Only add genuinely non-obvious learnings — things a future agent would likely get wrong
- Examples: "tests require running `make generate` first", "API routes must be registered in routes.go not main.go", "env vars are loaded from .env.local not .env"
- Keep entries as terse bullets
- Do NOT touch any section other than `## Implementation Notes`
- Do NOT modify any files other than CLAUDE.md
- If there's nothing worth recording, make NO changes