Determine the exact shell command to BUILD this project. Follow these steps:

1. Read CLAUDE.md if it exists — it often documents the build command directly
2. Read the project manifest (Package.swift, package.json, Cargo.toml, go.mod, pyproject.toml, Makefile, etc.)
3. Determine the correct build command based on what you read

CRITICAL RULES:
- Swift Package Manager with `platforms: [.iOS(...)]` and NO `.macOS(...)` in Package.swift: you MUST use xcodebuild, NOT `swift build`. `swift build` compiles for macOS where UIKit/SwiftUI are unavailable and WILL fail. Use: `xcodebuild -scheme '<PackageName>' -destination 'generic/platform=iOS Simulator' build`
- Swift Package Manager with `.macOS(...)` or no platforms declaration: use `swift build`
- Node.js: check for pnpm-lock.yaml (pnpm build), yarn.lock (yarn build), or default (npm run build)
- For monorepos, identify the primary build target

Output ONLY the exact shell command on a single line. No explanation, no markdown, no backticks, no quotes around the command. If no build system found, output: NONE