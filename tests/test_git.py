import os
import subprocess
import tempfile

import pytest

from autocoder.git import GitOps
from autocoder.types import LockError


@pytest.fixture
def git_repo():
    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(["git", "init"], cwd=tmpdir, capture_output=True, check=True)
        subprocess.run(["git", "checkout", "-b", "main"], cwd=tmpdir, capture_output=True, check=True)
        # Create initial commit
        filepath = os.path.join(tmpdir, "README.md")
        with open(filepath, "w") as f:
            f.write("# Test\n")
        subprocess.run(["git", "add", "."], cwd=tmpdir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmpdir, capture_output=True, check=True)
        yield tmpdir


def test_assert_clean(git_repo):
    git = GitOps(git_repo)
    git.assert_clean()  # Should not raise


def test_assert_clean_dirty(git_repo):
    with open(os.path.join(git_repo, "dirty.txt"), "w") as f:
        f.write("dirty")
    git = GitOps(git_repo)
    with pytest.raises(SystemExit):
        git.assert_clean()


def test_save_checkpoint(git_repo):
    git = GitOps(git_repo)
    sha = git.save_checkpoint()
    assert len(sha) == 40


def test_create_branch_and_checkout_main(git_repo):
    git = GitOps(git_repo)
    branch = git.create_branch(42, "Fix the widget")
    assert branch == "feat/42-fix-the-widget"
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=git_repo, capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "feat/42-fix-the-widget"
    git.checkout_main()
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=git_repo, capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "main"


def test_rollback(git_repo):
    git = GitOps(git_repo)
    sha = git.save_checkpoint()
    with open(os.path.join(git_repo, "new.txt"), "w") as f:
        f.write("new")
    subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "new"], cwd=git_repo, capture_output=True, check=True)
    git.rollback(sha)
    current = git.save_checkpoint()
    assert current == sha


def test_lockfile(git_repo):
    git = GitOps(git_repo)
    git.acquire_lock()
    git2 = GitOps(git_repo)
    with pytest.raises(LockError):
        git2.acquire_lock()
    git.release_lock()
    # Should succeed now
    git2.acquire_lock()
    git2.release_lock()


def test_cleanup_orphan_branches(git_repo):
    git = GitOps(git_repo)
    for branch in ("ai/issue-99", "feat/42-fix-bug"):
        subprocess.run(
            ["git", "checkout", "-b", branch],
            cwd=git_repo, capture_output=True, check=True,
        )
    subprocess.run(
        ["git", "checkout", "main"],
        cwd=git_repo, capture_output=True, check=True,
    )
    git.cleanup_orphan_branches()
    for pattern in ("ai/*", "feat/*"):
        result = subprocess.run(
            ["git", "branch", "--list", pattern],
            cwd=git_repo, capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == ""
