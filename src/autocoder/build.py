"""Auto-detect project type and return appropriate build command."""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def detect_build_cmd(repo_path: str) -> Optional[str]:
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
        return "swift build"

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
