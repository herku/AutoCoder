from __future__ import annotations

import re
from pathlib import Path

_DIR = Path(__file__).parent
_AGENT_MARKER_RE = re.compile(r"\{\{agent:([^}]+)\}\}")

# File-content cache keyed by resolved path, validated by mtime. Caching the
# raw reads (rather than the expanded result, as the old @lru_cache did)
# keeps long-lived processes (--serve) honest: adding or editing a repo
# override — or an {{agent:X}} file it expands — takes effect on the next
# load, no restart needed. Expansion itself is cheap regex work.
_read_cache: dict[Path, tuple[float, str]] = {}


def load(name: str, repo_path: str | None = None) -> str:
    """Load a prompt template by name (without .md extension).

    Resolution: <repo_path>/.autocoder/prompts/<name>.md wins over the
    packaged default. {{agent:X}} markers are expanded recursively by
    loading agents/X through the same resolution, with braces in the
    expanded content doubled so later str.format() calls leave it alone.
    """
    content = _read(name, repo_path)

    def expand(match: re.Match[str]) -> str:
        agent_name = match.group(1).strip()
        agent_content = load(f"agents/{agent_name}", repo_path)
        return agent_content.replace("{", "{{").replace("}", "}}")

    return _AGENT_MARKER_RE.sub(expand, content)


def _cache_clear() -> None:
    _read_cache.clear()


# Backward-compatible with the previous @lru_cache implementation.
load.cache_clear = _cache_clear  # type: ignore[attr-defined]


def _read(name: str, repo_path: str | None) -> str:
    if repo_path is not None:
        override = Path(repo_path) / ".autocoder" / "prompts" / f"{name}.md"
        if override.exists():
            return _read_file(override)
    return _read_file(_DIR / f"{name}.md")


def _read_file(path: Path) -> str:
    mtime = path.stat().st_mtime
    cached = _read_cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    text = path.read_text().rstrip("\n")
    _read_cache[path] = (mtime, text)
    return text
