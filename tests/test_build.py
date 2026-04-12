"""Tests for build command auto-detection."""

from autocoder.build import detect_build_cmd


def test_detect_nodejs_npm(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert detect_build_cmd(str(tmp_path)) == "npm run build"


def test_detect_nodejs_npm_with_lockfile(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "package-lock.json").write_text("{}")
    assert detect_build_cmd(str(tmp_path)) == "npm run build"


def test_detect_nodejs_yarn(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "yarn.lock").write_text("")
    assert detect_build_cmd(str(tmp_path)) == "yarn build"


def test_detect_nodejs_pnpm(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "pnpm-lock.yaml").write_text("")
    assert detect_build_cmd(str(tmp_path)) == "pnpm build"


def test_detect_rust(tmp_path):
    (tmp_path / "Cargo.toml").write_text("")
    assert detect_build_cmd(str(tmp_path)) == "cargo build"


def test_detect_go(tmp_path):
    (tmp_path / "go.mod").write_text("")
    assert detect_build_cmd(str(tmp_path)) == "go build ./..."


def test_detect_swift_package(tmp_path):
    (tmp_path / "Package.swift").write_text("")
    assert detect_build_cmd(str(tmp_path)) == "swift build"


def test_detect_xcode(tmp_path):
    (tmp_path / "MyApp.xcodeproj").mkdir()
    assert detect_build_cmd(str(tmp_path)) == "xcodebuild"


def test_swift_package_takes_priority_over_xcodeproj(tmp_path):
    (tmp_path / "Package.swift").write_text("")
    (tmp_path / "MyApp.xcodeproj").mkdir()
    assert detect_build_cmd(str(tmp_path)) == "swift build"


def test_detect_none(tmp_path):
    assert detect_build_cmd(str(tmp_path)) is None


def test_detect_python_uv(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "uv.lock").write_text("")
    assert detect_build_cmd(str(tmp_path)) == "uv build"


def test_detect_python_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    assert detect_build_cmd(str(tmp_path)) == "python -m build"


def test_detect_python_setup_py(tmp_path):
    (tmp_path / "setup.py").write_text("")
    assert detect_build_cmd(str(tmp_path)) == "python setup.py build"


def test_uv_takes_priority_over_plain_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "uv.lock").write_text("")
    assert detect_build_cmd(str(tmp_path)) == "uv build"


def test_pnpm_takes_priority_over_yarn(tmp_path):
    """When both pnpm and yarn lockfiles exist, pnpm wins."""
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "yarn.lock").write_text("")
    assert detect_build_cmd(str(tmp_path)) == "pnpm build"
