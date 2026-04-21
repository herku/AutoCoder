"""Tests for build command auto-detection."""

import json
from unittest.mock import patch, MagicMock

from autocoder.build import detect_build_cmd, _detect_build_cmd_heuristic


# --- Heuristic detection tests ---

def test_detect_nodejs_npm(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert _detect_build_cmd_heuristic(str(tmp_path)) == "npm run build"


def test_detect_nodejs_npm_with_lockfile(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "package-lock.json").write_text("{}")
    assert _detect_build_cmd_heuristic(str(tmp_path)) == "npm run build"


def test_detect_nodejs_yarn(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "yarn.lock").write_text("")
    assert _detect_build_cmd_heuristic(str(tmp_path)) == "yarn build"


def test_detect_nodejs_pnpm(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "pnpm-lock.yaml").write_text("")
    assert _detect_build_cmd_heuristic(str(tmp_path)) == "pnpm build"


def test_detect_rust(tmp_path):
    (tmp_path / "Cargo.toml").write_text("")
    assert _detect_build_cmd_heuristic(str(tmp_path)) == "cargo build"


def test_detect_go(tmp_path):
    (tmp_path / "go.mod").write_text("")
    assert _detect_build_cmd_heuristic(str(tmp_path)) == "go build ./..."


def test_detect_swift_package(tmp_path):
    (tmp_path / "Package.swift").write_text("")
    assert _detect_build_cmd_heuristic(str(tmp_path)) == "swift build"


def test_detect_swift_package_macos_only(tmp_path):
    (tmp_path / "Package.swift").write_text('platforms: [.macOS(.v13)]')
    assert _detect_build_cmd_heuristic(str(tmp_path)) == "swift build"


def test_detect_swift_package_cross_platform(tmp_path):
    (tmp_path / "Package.swift").write_text('platforms: [.iOS(.v16), .macOS(.v13)]')
    assert _detect_build_cmd_heuristic(str(tmp_path)) == "swift build"


def test_detect_swift_package_ios_only(tmp_path):
    (tmp_path / "Package.swift").write_text(
        'let package = Package(\n    name: "FeedbackBoard",\n    platforms: [.iOS(.v16)],\n)'
    )
    result = _detect_build_cmd_heuristic(str(tmp_path))
    assert "xcodebuild" in result
    assert "FeedbackBoard" in result
    assert "iOS Simulator" in result


def test_detect_swift_package_ios_only_extracts_scheme(tmp_path):
    (tmp_path / "Package.swift").write_text(
        'let package = Package(\n    name: "MySDK",\n    platforms: [.iOS(.v15)],\n)'
    )
    result = _detect_build_cmd_heuristic(str(tmp_path))
    assert "-scheme 'MySDK'" in result


def test_detect_swift_package_ios_only_fallback_dir_name(tmp_path):
    (tmp_path / "Package.swift").write_text('platforms: [.iOS(.v16)]')
    result = _detect_build_cmd_heuristic(str(tmp_path))
    assert "xcodebuild" in result
    assert tmp_path.name in result


def test_detect_xcode(tmp_path):
    (tmp_path / "MyApp.xcodeproj").mkdir()
    assert _detect_build_cmd_heuristic(str(tmp_path)) == "xcodebuild"


def test_swift_package_takes_priority_over_xcodeproj(tmp_path):
    (tmp_path / "Package.swift").write_text("")
    (tmp_path / "MyApp.xcodeproj").mkdir()
    assert _detect_build_cmd_heuristic(str(tmp_path)) == "swift build"


def test_detect_none(tmp_path):
    assert _detect_build_cmd_heuristic(str(tmp_path)) is None


def test_detect_python_uv(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "uv.lock").write_text("")
    assert _detect_build_cmd_heuristic(str(tmp_path)) == "uv build"


def test_detect_python_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    assert _detect_build_cmd_heuristic(str(tmp_path)) == "python -m build"


def test_detect_python_setup_py(tmp_path):
    (tmp_path / "setup.py").write_text("")
    assert _detect_build_cmd_heuristic(str(tmp_path)) == "python setup.py build"


def test_uv_takes_priority_over_plain_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "uv.lock").write_text("")
    assert _detect_build_cmd_heuristic(str(tmp_path)) == "uv build"


def test_pnpm_takes_priority_over_yarn(tmp_path):
    """When both pnpm and yarn lockfiles exist, pnpm wins."""
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "yarn.lock").write_text("")
    assert _detect_build_cmd_heuristic(str(tmp_path)) == "pnpm build"


# --- AI-first detection tests ---

def _mock_claude_response(text: str) -> MagicMock:
    """Create a mock subprocess result with Claude JSON output."""
    mock = MagicMock()
    mock.returncode = 0
    mock.stdout = json.dumps({"result": text, "is_error": False})
    mock.stderr = ""
    return mock


def _mock_claude_failure() -> MagicMock:
    mock = MagicMock()
    mock.returncode = 1
    mock.stdout = ""
    mock.stderr = "error"
    return mock


@patch("autocoder.build.subprocess.run")
def test_ai_result_used_first(mock_run, tmp_path):
    """AI result takes priority over heuristic."""
    (tmp_path / "Cargo.toml").write_text("")
    mock_run.return_value = _mock_claude_response("cargo build --release")
    result = detect_build_cmd(str(tmp_path), "claude-sonnet-4-6")
    assert result == "cargo build --release"
    mock_run.assert_called_once()


@patch("autocoder.build.subprocess.run")
def test_ai_xcodebuild_for_ios_package(mock_run, tmp_path):
    """AI correctly returns xcodebuild for iOS-only SPM packages."""
    mock_run.return_value = _mock_claude_response(
        "xcodebuild -scheme 'FeedbackBoard' -destination 'generic/platform=iOS Simulator' build"
    )
    result = detect_build_cmd(str(tmp_path), "claude-sonnet-4-6")
    assert "xcodebuild" in result
    assert "iOS Simulator" in result


@patch("autocoder.build.subprocess.run")
def test_heuristic_fallback_when_ai_fails(mock_run, tmp_path):
    """Heuristic used when AI returns non-zero exit."""
    mock_run.return_value = _mock_claude_failure()
    (tmp_path / "Cargo.toml").write_text("")
    result = detect_build_cmd(str(tmp_path), "claude-sonnet-4-6")
    assert result == "cargo build"


@patch("autocoder.build.subprocess.run")
def test_heuristic_fallback_when_ai_exception(mock_run, tmp_path):
    """Heuristic used when AI throws exception."""
    mock_run.side_effect = Exception("connection failed")
    (tmp_path / "package.json").write_text("{}")
    result = detect_build_cmd(str(tmp_path), "claude-sonnet-4-6")
    assert result == "npm run build"


@patch("autocoder.build.subprocess.run")
def test_ai_strips_markdown_fences(mock_run, tmp_path):
    mock_run.return_value = _mock_claude_response("`make build`")
    result = detect_build_cmd(str(tmp_path), "claude-sonnet-4-6")
    assert result == "make build"


@patch("autocoder.build.subprocess.run")
def test_ai_none_falls_through_to_heuristic(mock_run, tmp_path):
    """AI returning NONE triggers heuristic fallback."""
    mock_run.return_value = _mock_claude_response("NONE")
    (tmp_path / "go.mod").write_text("")
    result = detect_build_cmd(str(tmp_path), "claude-sonnet-4-6")
    assert result == "go build ./..."


@patch("autocoder.build.subprocess.run")
def test_both_fail_returns_none(mock_run, tmp_path):
    """None returned when both AI and heuristic fail."""
    mock_run.return_value = _mock_claude_response("NONE")
    result = detect_build_cmd(str(tmp_path), "claude-sonnet-4-6")
    assert result is None
