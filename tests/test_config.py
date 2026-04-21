import pytest

from autocoder.config import parse_duration, resolve_external_reviewer


def test_parse_duration_none():
    assert parse_duration(None) is None
    assert parse_duration("") is None


def test_parse_duration_seconds():
    assert parse_duration("30s") == 30
    assert parse_duration("30") == 30


def test_parse_duration_minutes():
    assert parse_duration("5m") == 300
    assert parse_duration("1m") == 60


def test_parse_duration_hours():
    assert parse_duration("1h") == 3600
    assert parse_duration("2h") == 7200


def test_parse_duration_case_insensitive():
    assert parse_duration("5M") == 300
    assert parse_duration("1H") == 3600


def test_parse_duration_invalid():
    with pytest.raises(SystemExit):
        parse_duration("garbage")
    with pytest.raises(SystemExit):
        parse_duration("5d")  # day suffix not supported


def test_resolve_external_reviewer_none():
    assert resolve_external_reviewer(None) is None
    assert resolve_external_reviewer("") is None


def test_resolve_external_reviewer_presets():
    assert resolve_external_reviewer("codex") == ["codex", "exec"]
    assert resolve_external_reviewer("gemini") == ["gemini"]
    assert resolve_external_reviewer("claude") == ["claude", "-p", "--output-format", "text"]


def test_resolve_external_reviewer_raw_command_passes_through():
    assert resolve_external_reviewer("codex exec -m gpt-5") == ["codex", "exec", "-m", "gpt-5"]
    assert resolve_external_reviewer("claude -p --output-format text") == [
        "claude", "-p", "--output-format", "text",
    ]


def test_resolve_external_reviewer_unknown_single_token_passes_through():
    assert resolve_external_reviewer("unknownname") == ["unknownname"]
