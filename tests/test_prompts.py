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


def test_load_picks_up_override_edits_without_cache_clear(tmp_path):
    """Long-lived (--serve) processes must see live prompt-override edits."""
    import os

    prompts_dir = tmp_path / ".autocoder" / "prompts"
    prompts_dir.mkdir(parents=True)
    override = prompts_dir / "review.md"
    override.write_text("FIRST {diff}\n")
    assert load("review", str(tmp_path)).startswith("FIRST")

    override.write_text("SECOND {diff}\n")
    # Force a distinct mtime even on coarse-grained filesystems.
    os.utime(override, (override.stat().st_atime, override.stat().st_mtime + 10))
    assert load("review", str(tmp_path)).startswith("SECOND")


def test_load_picks_up_override_added_after_first_load(tmp_path):
    """The absence of an override must not be memoized forever."""
    prompts_dir = tmp_path / ".autocoder" / "prompts"
    prompts_dir.mkdir(parents=True)
    assert "code reviewer" in load("review", str(tmp_path)).lower()

    (prompts_dir / "review.md").write_text("LATE OVERRIDE {diff}\n")
    assert load("review", str(tmp_path)).startswith("LATE OVERRIDE")


def test_load_picks_up_agent_file_edits(tmp_path):
    import os

    prompts_dir = tmp_path / ".autocoder" / "prompts"
    (prompts_dir / "agents").mkdir(parents=True)
    (prompts_dir / "orch.md").write_text("{{agent:role}}\n")
    agent_file = prompts_dir / "agents" / "role.md"
    agent_file.write_text("ROLE_V1\n")
    assert "ROLE_V1" in load("orch", str(tmp_path))

    agent_file.write_text("ROLE_V2\n")
    os.utime(agent_file, (agent_file.stat().st_atime, agent_file.stat().st_mtime + 10))
    assert "ROLE_V2" in load("orch", str(tmp_path))
