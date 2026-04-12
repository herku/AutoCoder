You just fixed a CI failure. Review what happened and capture any learnings in CLAUDE.md.

## CI failure output
{ci_output}

## Changes applied to fix it
{fix_diff}

## Instructions
- Read the current CLAUDE.md
- If a `## CI / Build Notes` section exists, merge new findings into it. Otherwise append one.
- Only add genuinely non-obvious learnings — things the agent got wrong or would likely get wrong again
- Examples: "must run `go mod tidy` after adding deps", "CI uses Go 1.21, not 1.22", "lint requires gofumpt not gofmt"
- Keep entries as terse bullets
- Do NOT touch any section other than `## CI / Build Notes`
- Do NOT modify any files other than CLAUDE.md
- If there's nothing worth recording, make NO changes