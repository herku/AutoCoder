import os
import stat
import subprocess
import tempfile

import pytest

from autocoder.anticheat import (
    AntiCheatViolation,
    audit_diff,
    protect_test_files,
    restore_test_files,
)


@pytest.fixture
def git_repo_with_tests():
    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(["git", "init"], cwd=tmpdir, capture_output=True, check=True)
        subprocess.run(["git", "checkout", "-b", "main"], cwd=tmpdir, capture_output=True, check=True)

        # Create source and test files
        os.makedirs(os.path.join(tmpdir, "src"))
        os.makedirs(os.path.join(tmpdir, "tests"))

        for path, content in [
            ("src/app.py", "def main(): pass"),
            ("tests/test_app.py", "def test_main(): pass"),
        ]:
            with open(os.path.join(tmpdir, path), "w") as f:
                f.write(content)

        subprocess.run(["git", "add", "."], cwd=tmpdir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmpdir, capture_output=True, check=True)
        yield tmpdir


def test_protect_and_restore(git_repo_with_tests):
    patterns = ["**/test_*"]
    protected = protect_test_files(git_repo_with_tests, patterns)
    assert len(protected) == 1

    test_file = protected[0]
    mode = os.stat(test_file).st_mode
    assert not (mode & stat.S_IWUSR)  # Not writable

    restore_test_files(protected)
    mode = os.stat(test_file).st_mode
    assert mode & stat.S_IWUSR  # Writable again


def test_audit_diff_no_violation(git_repo_with_tests):
    # Modify only source file
    with open(os.path.join(git_repo_with_tests, "src/app.py"), "w") as f:
        f.write("def main(): return 42")
    # audit_diff checks uncommitted changes against HEAD
    audit_diff(git_repo_with_tests, ["**/test_*"])  # Should not raise


def test_audit_diff_violation(git_repo_with_tests):
    # Modify test file
    with open(os.path.join(git_repo_with_tests, "tests/test_app.py"), "w") as f:
        f.write("def test_main(): assert True")
    with pytest.raises(AntiCheatViolation):
        audit_diff(git_repo_with_tests, ["**/test_*"])
