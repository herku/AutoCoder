"""Auto-detect project build command via AI analysis with heuristic fallback."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Optional

from autocoder.prompts import load

DETECT_BUDGET = 0.50  # $0.50 max for build detection
DETECT_TIMEOUT = 120  # 2 minutes


def detect_build_cmd(repo_path: str, model: str) -> Optional[str]:
    """Detect build command: AI first (reads project files), heuristic fallback."""
    try:
        result = _detect_build_cmd_ai(repo_path, model)
        if result:
            print(f"  Build command (AI): {result}")
            return result
    except Exception as e:
        print(f"  AI build detection failed ({e}), falling back to heuristic")

    result = _detect_build_cmd_heuristic(repo_path)
    if result:
        print(f"  Build command (heuristic): {result}")
    return result


def _detect_build_cmd_ai(repo_path: str, model: str) -> Optional[str]:
    """Use claude -p to analyze repo and determine build command."""
    from autocoder.sandbox import SandboxConfig, build_claude_cmd

    prompt = load("detect_build")
    sandbox = SandboxConfig(
        allowed_tools=["Read", "Glob", "Grep", "Bash(git diff:*)", "Bash(git status:*)", "Bash(git log:*)"],
        docker=False,
    )
    cmd = build_claude_cmd(model, "low", DETECT_BUDGET, sandbox, repo_path)

    result = subprocess.run(
        cmd,
        input=prompt,
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=DETECT_TIMEOUT,
    )

    if result.returncode != 0:
        return None

    # Parse JSON response from claude
    try:
        data = json.loads(result.stdout)
        text = data.get("result", "") or ""
        if isinstance(text, list):
            text = "\n".join(
                block.get("text", "") for block in text if block.get("type") == "text"
            )
    except json.JSONDecodeError:
        text = result.stdout

    # Extract the build command — should be a single line
    cmd_text = text.strip().splitlines()[-1].strip() if text.strip() else ""

    if not cmd_text or cmd_text.upper() == "NONE":
        return None

    # Strip markdown code fences if AI wrapped the output
    if cmd_text.startswith("`") and cmd_text.endswith("`"):
        cmd_text = cmd_text.strip("`")

    return cmd_text


def _detect_swift_build_cmd(root: Path) -> str:
    """Return the correct build command for a Swift package, handling iOS-only targets."""
    content = (root / "Package.swift").read_text()

    has_ios = bool(re.search(r'\.iOS\s*\(', content))
    has_macos = bool(re.search(r'\.macOS\s*\(', content))

    if has_ios and not has_macos:
        # iOS-only package: swift build compiles for macOS host where UIKit/SwiftUI don't work.
        # Must use xcodebuild with iOS Simulator destination.
        name_match = re.search(r'name:\s*"([^"]+)"', content)
        scheme = name_match.group(1) if name_match else root.name
        return f"xcodebuild -scheme '{scheme}' -destination 'generic/platform=iOS Simulator' build"

    return "swift build"


def _detect_build_cmd_heuristic(repo_path: str) -> Optional[str]:
    """Return the appropriate build command for the project, or None if undetectable."""
    root = Path(repo_path)

    # Node.js: check lockfiles to pick package manager
    if (root / "package.json").exists():
        if (root / "pnpm-lock.yaml").exists():
            return "pnpm build"
        if (root / "yarn.lock").exists():
            return "yarn build"
        return "npm run build"

    # Rust
    if (root / "Cargo.toml").exists():
        return "cargo build"

    # Go
    if (root / "go.mod").exists():
        return "go build ./..."

    # Swift: Package.swift takes priority over .xcodeproj
    if (root / "Package.swift").exists():
        return _detect_swift_build_cmd(root)

    xcodeprojs = list(root.glob("*.xcodeproj"))
    if xcodeprojs:
        return "xcodebuild"

    # Python: uv-managed takes priority, then standard pyproject.toml, then setup.py
    if (root / "pyproject.toml").exists():
        if (root / "uv.lock").exists():
            return "uv build"
        return "python -m build"
    if (root / "setup.py").exists():
        return "python setup.py build"

    return None
