from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).parent


@lru_cache(maxsize=None)
def load(name: str) -> str:
    """Load a prompt template by name (without .md extension)."""
    return (_DIR / f"{name}.md").read_text().rstrip("\n")
