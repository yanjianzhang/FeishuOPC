"""B-3 integration test — real ``git worktree`` round-trip.

Validates the property that makes B-3 worth shipping: two
worktrees on distinct agent branches can commit concurrently
without corrupting either and without touching the main working
copy. The fallback path is asserted indirectly — if
``enable_worktree_isolation=False`` the same test would serialise
on ``repo_filelock``, but we don't time-assert here; the timing
claim lives in the unit suite.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from feishu_agent.team.worktree_manager import WorktreeManager


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not installed on host"
)


def _run(cwd: Path, *argv: str) -> str:
    return subprocess.run(
        list(argv),
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "git", "init", "--initial-branch=main")
    _run(repo, "git", "config", "user.email", "test@example.com")
    _run(repo, "git", "config", "user.name", "test")
    (repo / "README.md").write_text("hello\n")
    _run(repo, "git", "add", ".")
    _run(repo, "git", "commit", "-m", "init")
    return repo


def test_two_worktrees_commit_independently(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    mgr = WorktreeManager(repo)

    wt_a = mgr.acquire("agent-A")
    wt_b = mgr.acquire("agent-B")
    assert not wt_a.is_fallback
    assert not wt_b.is_fallback
    assert wt_a.path != wt_b.path

    # Commit in both worktrees. Under B-3 these never touch main
    # WC's index; the only shared resource is .git/ metadata, which
    # git handles via its own locking. No repo_filelock needed on
    # the commits themselves.
    (wt_a.path / "a.txt").write_text("A\n")
    _run(wt_a.path, "git", "add", "a.txt")
    _run(wt_a.path, "git", "commit", "-m", "A change")

    (wt_b.path / "b.txt").write_text("B\n")
    _run(wt_b.path, "git", "add", "b.txt")
    _run(wt_b.path, "git", "commit", "-m", "B change")

    # Main branch HEAD is unchanged.
    main_log = _run(repo, "git", "log", "--oneline", "main")
    assert "init" in main_log
    assert "A change" not in main_log
    assert "B change" not in main_log

    # Each agent branch contains exactly its own commit on top of init.
    log_a = _run(repo, "git", "log", "--oneline", wt_a.branch)
    log_b = _run(repo, "git", "log", "--oneline", wt_b.branch)
    assert "A change" in log_a
    assert "B change" not in log_a
    assert "B change" in log_b
    assert "A change" not in log_b

    # Story 004.5 AC-5 — both worktree dirs MUST still exist until
    # release() is called. This pins the "keep the isolated checkout
    # around for the TL's merge step" invariant; without it a future
    # refactor that eagerly deletes on commit would silently break
    # the dispatch pipeline.
    assert wt_a.path.exists(), "worktree A removed before release()"
    assert wt_b.path.exists(), "worktree B removed before release()"
    # Commit count on each agent branch: exactly init + one child commit.
    count_a = len(
        _run(repo, "git", "log", "--oneline", wt_a.branch).splitlines()
    )
    count_b = len(
        _run(repo, "git", "log", "--oneline", wt_b.branch).splitlines()
    )
    assert count_a == 2, f"expected 2 commits on {wt_a.branch}, got {count_a}"
    assert count_b == 2, f"expected 2 commits on {wt_b.branch}, got {count_b}"


def test_release_cleans_up_successful_worktree(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    mgr = WorktreeManager(repo)
    handle = mgr.acquire("agent-clean")
    assert handle.path.exists()
    removed = mgr.release(handle, success=True)
    assert removed is True
    assert not handle.path.exists()


def test_release_retains_worktree_on_failure(tmp_path: Path) -> None:
    """keep_on_failure=True (default) means a crashed child leaves
    the worktree on disk for an operator to inspect."""
    repo = _init_repo(tmp_path)
    mgr = WorktreeManager(repo)
    handle = mgr.acquire("agent-failed")
    (handle.path / "wip.txt").write_text("midway\n")
    removed = mgr.release(handle, success=False)
    assert removed is False
    assert handle.path.exists()
    assert (handle.path / "wip.txt").exists()


def test_disabled_manager_returns_main_repo(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    mgr = WorktreeManager(repo, enabled=False)
    handle = mgr.acquire("agent-off")
    assert handle.is_fallback is True
    assert handle.path == repo
    # No .worktrees/ side effect.
    assert not (repo / ".worktrees").exists()
