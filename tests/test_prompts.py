from pathlib import Path

import pytest

from autocoder.prompts import load


@pytest.fixture(autouse=True)
def _clear_cache():
    load.cache_clear()
    yield
    load.cache_clear()


def test_load_package_default():
    text = load("review")
    assert "code reviewer" in text.lower()


def test_load_project_override_wins(tmp_path):
    prompts_dir = tmp_path / ".autocoder" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "review.md").write_text("OVERRIDDEN {diff}\n")

    text = load("review", str(tmp_path))
    assert text.startswith("OVERRIDDEN")
    assert "code reviewer" not in text.lower()


def test_load_missing_override_falls_back(tmp_path):
    (tmp_path / ".autocoder" / "prompts").mkdir(parents=True)
    # No review.md placed — should fall back to package
    text = load("review", str(tmp_path))
    assert "code reviewer" in text.lower()


def test_load_cache_keyed_per_repo(tmp_path):
    a = tmp_path / "repo_a"
    b = tmp_path / "repo_b"
    (a / ".autocoder" / "prompts").mkdir(parents=True)
    (b / ".autocoder" / "prompts").mkdir(parents=True)
    (a / ".autocoder" / "prompts" / "review.md").write_text("A-VERSION\n")
    (b / ".autocoder" / "prompts" / "review.md").write_text("B-VERSION\n")

    assert load("review", str(a)).startswith("A-VERSION")
    assert load("review", str(b)).startswith("B-VERSION")
    # And no override path returns the package default
    assert "code reviewer" in load("review").lower()


def test_agent_marker_expansion(tmp_path):
    prompts_dir = tmp_path / ".autocoder" / "prompts"
    (prompts_dir / "agents").mkdir(parents=True)
    (prompts_dir / "orch.md").write_text("START\n{{agent:role_x}}\nEND\n")
    (prompts_dir / "agents" / "role_x.md").write_text("ROLE_X_BODY\n")

    text = load("orch", str(tmp_path))
    assert "START" in text
    assert "ROLE_X_BODY" in text
    assert "END" in text
    assert "{{agent:role_x}}" not in text


def test_agent_marker_escapes_braces(tmp_path):
    """Sub-agent content with { or } must survive a later str.format() call."""
    prompts_dir = tmp_path / ".autocoder" / "prompts"
    (prompts_dir / "agents").mkdir(parents=True)
    (prompts_dir / "orch.md").write_text("{{agent:role}} and {diff}\n")
    (prompts_dir / "agents" / "role.md").write_text('example: {"key": "value"}\n')

    text = load("orch", str(tmp_path))
    # Braces in the sub-content should be doubled so format() preserves them
    formatted = text.format(diff="DIFF")
    assert 'example: {"key": "value"}' in formatted
    assert "DIFF" in formatted


def test_agent_marker_override_also_overridable(tmp_path):
    """If a project overrides both the base AND an agent file, both override."""
    prompts_dir = tmp_path / ".autocoder" / "prompts"
    (prompts_dir / "agents").mkdir(parents=True)
    (prompts_dir / "orch.md").write_text("{{agent:role}}\n")
    (prompts_dir / "agents" / "role.md").write_text("OVERRIDDEN_ROLE\n")

    text = load("orch", str(tmp_path))
    assert "OVERRIDDEN_ROLE" in text
