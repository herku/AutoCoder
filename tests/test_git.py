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


def _lockfile_path(git_repo):
    return os.path.join(git_repo, ".autocoder", ".autocoder.lock")


def _write_lockfile(git_repo, content):
    os.makedirs(os.path.dirname(_lockfile_path(git_repo)), exist_ok=True)
    with open(_lockfile_path(git_repo), "w") as f:
        f.write(content)


def test_stale_lock_dead_pid_auto_recovers(git_repo):
    proc = subprocess.Popen(["sleep", "0"])
    proc.wait()  # reaped — the PID no longer exists
    _write_lockfile(git_repo, str(proc.pid))
    git = GitOps(git_repo)
    git.acquire_lock()
    with open(_lockfile_path(git_repo)) as f:
        assert f.read().strip() == str(os.getpid())
    git.release_lock()


def test_live_pid_lock_still_raises(git_repo):
    _write_lockfile(git_repo, str(os.getpid()))
    git = GitOps(git_repo)
    with pytest.raises(LockError, match="Remove"):
        git.acquire_lock()
    # Live lock must not be auto-removed
    assert os.path.exists(_lockfile_path(git_repo))


def test_corrupt_lock_raises_not_removed(git_repo):
    _write_lockfile(git_repo, "garbage")
    git = GitOps(git_repo)
    with pytest.raises(LockError, match="unknown"):
        git.acquire_lock()
    assert os.path.exists(_lockfile_path(git_repo))


def test_release_does_not_remove_foreign_lock(git_repo):
    git = GitOps(git_repo)
    git.acquire_lock()
    _write_lockfile(git_repo, str(os.getpid() + 1))
    git.release_lock()
    assert os.path.exists(_lockfile_path(git_repo))


def test_double_acquire_same_instance_is_noop(git_repo):
    git = GitOps(git_repo)
    git.acquire_lock()
    git.acquire_lock()  # must not raise
    git.release_lock()


def test_acquire_writes_autocoder_gitignore(git_repo):
    git = GitOps(git_repo)
    git.acquire_lock()
    gitignore = os.path.join(git_repo, ".autocoder", ".gitignore")
    with open(gitignore) as f:
        assert f.read().strip() == "*"
    git.release_lock()


def test_commit_all_excludes_autocoder_dir(git_repo):
    images_dir = os.path.join(git_repo, ".autocoder", "images", "issue-1")
    os.makedirs(images_dir)
    with open(os.path.join(images_dir, "x.png"), "wb") as f:
        f.write(b"\x89PNG")
    with open(os.path.join(git_repo, "src.py"), "w") as f:
        f.write("print('hi')\n")
    git = GitOps(git_repo)
    git.commit_all("add source")
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=git_repo, capture_output=True, text=True, check=True,
    )
    files = result.stdout.split()
    assert "src.py" in files
    assert not any(f.startswith(".autocoder/") for f in files)


def test_cleanup_orphan_branches(git_repo):
    git = GitOps(git_repo)
    autocoder_branches = ("ai/issue-99", "feat/42-fix-bug", "feat/7", "autocoder-wt-x-3")
    human_branches = ("feat/nice-ui", "feat/refactor-auth")
    for branch in autocoder_branches + human_branches:
        subprocess.run(
            ["git", "checkout", "-b", branch],
            cwd=git_repo, capture_output=True, check=True,
        )
    subprocess.run(
        ["git", "checkout", "main"],
        cwd=git_repo, capture_output=True, check=True,
    )
    git.cleanup_orphan_branches()
    result = subprocess.run(
        ["git", "branch", "--list", "--format=%(refname:short)"],
        cwd=git_repo, capture_output=True, text=True, check=True,
    )
    remaining = set(result.stdout.split())
    # AutoCoder-shaped branches removed...
    for branch in autocoder_branches:
        assert branch not in remaining
    # ...human feat/ branches untouched.
    for branch in human_branches:
        assert branch in remaining
