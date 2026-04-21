from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent
_AGENT_MARKER_RE = re.compile(r"\{\{agent:([^}]+)\}\}")


@lru_cache(maxsize=None)
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


def _read(name: str, repo_path: str | None) -> str:
    if repo_path is not None:
        override = Path(repo_path) / ".autocoder" / "prompts" / f"{name}.md"
        if override.exists():
            return override.read_text().rstrip("\n")
    return (_DIR / f"{name}.md").read_text().rstrip("\n")
