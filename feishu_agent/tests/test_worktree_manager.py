"""B-3 WorktreeManager unit tests.

Unit-level coverage uses a fake git runner so we can exercise the
fallback/idempotence/release branches without touching a real
``.git/`` directory. Integration coverage (real-git round-trip)
lives in ``test_worktree_concurrent_commits.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from feishu_agent.team.worktree_manager import (
    WorktreeHandle,
    WorktreeManager,
)


# ---------------------------------------------------------------------------
# Fake git runner
# ---------------------------------------------------------------------------


class FakeGit:
    """Records ``argv`` + ``cwd`` for each call. Can be primed with a
    list of exit codes to simulate failure paths. The default
    behaviour — ``succeed_forever()`` — just creates the target
    worktree directory when it sees ``worktree add`` so the
    idempotence check in subsequent acquires observes the filesystem
    side-effect of the first successful call."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self._fail_next: list[int] = []

    def fail_next(self, returncode: int = 128) -> None:
        self._fail_next.append(returncode)

    def __call__(
        self, argv: list[str], cwd: Path
    ) -> subprocess.CompletedProcess:
        self.calls.append((argv, cwd))
        if self._fail_next:
            rc = self._fail_next.pop(0)
            raise subprocess.CalledProcessError(
                rc, argv, stderr="simulated failure"
            )
        # Simulate the directory creation that real git would do so
        # the idempotence path in the second acquire() sees it.
        if len(argv) >= 5 and argv[0] == "worktree" and argv[1] == "add":
            # argv layout: ["worktree", "add", "-b", branch, path, base]
            wt_path = Path(argv[4])
            wt_path.mkdir(parents=True, exist_ok=True)
        if (
            len(argv) >= 3
            and argv[0] == "worktree"
            and argv[1] == "remove"
        ):
            wt_path = Path(argv[-1])
            if wt_path.exists():
                # Mimic `git worktree remove --force` which deletes
                # the directory.
                import shutil

                shutil.rmtree(wt_path, ignore_errors=True)
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def fake_git() -> FakeGit:
    return FakeGit()


@pytest.fixture
def manager(tmp_path: Path, fake_git: FakeGit) -> WorktreeManager:
    (tmp_path / ".git").mkdir()  # for _repo_filelock to work
    return WorktreeManager(tmp_path, git=fake_git)


# ---------------------------------------------------------------------------
# acquire
# ---------------------------------------------------------------------------


def test_acquire_creates_worktree_path_and_branch(
    tmp_path: Path, manager: WorktreeManager, fake_git: FakeGit
) -> None:
    handle = manager.acquire("trace-1")
    assert handle.path == tmp_path / ".worktrees" / "trace-1"
    assert handle.branch == "agent/trace-1"
    assert handle.is_fallback is False
    assert fake_git.calls
    argv, _ = fake_git.calls[-1]
    assert argv[:4] == [
        "worktree",
        "add",
        "-b",
        "agent/trace-1",
    ]


def test_acquire_is_idempotent_for_same_trace(
    tmp_path: Path, manager: WorktreeManager, fake_git: FakeGit
) -> None:
    first = manager.acquire("trace-1")
    calls_after_first = len(fake_git.calls)
    second = manager.acquire("trace-1")
    # Same path, same branch, no extra git call (dir already exists).
    assert first.path == second.path
    assert first.branch == second.branch
    assert len(fake_git.calls) == calls_after_first


def test_acquire_returns_fallback_when_disabled(
    tmp_path: Path, fake_git: FakeGit
) -> None:
    (tmp_path / ".git").mkdir()
    mgr = WorktreeManager(tmp_path, git=fake_git, enabled=False)
    handle = mgr.acquire("trace-x")
    assert handle.is_fallback is True
    assert handle.path == tmp_path
    # Disabled manager does not invoke git at all.
    assert fake_git.calls == []


def test_acquire_with_empty_trace_id_falls_back(
    manager: WorktreeManager, fake_git: FakeGit
) -> None:
    handle = manager.acquire("")
    assert handle.is_fallback is True
    assert fake_git.calls == []


def test_acquire_falls_back_when_git_fails(
    tmp_path: Path, manager: WorktreeManager, fake_git: FakeGit
) -> None:
    fake_git.fail_next(returncode=128)
    handle = manager.acquire("trace-bad")
    assert handle.is_fallback is True
    # We did *attempt* the git call before falling back.
    assert len(fake_git.calls) == 1


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


def test_release_fallback_is_noop(
    manager: WorktreeManager, fake_git: FakeGit
) -> None:
    fake_git.fail_next()
    handle = manager.acquire("trace-bad")
    assert handle.is_fallback
    fake_git.calls.clear()
    removed = manager.release(handle)
    assert removed is False
    assert fake_git.calls == []


def test_release_keeps_on_failure_by_default(
    manager: WorktreeManager, fake_git: FakeGit
) -> None:
    handle = manager.acquire("trace-keep")
    fake_git.calls.clear()
    removed = manager.release(handle, success=False)
    assert removed is False
    assert fake_git.calls == []
    assert handle.path.exists()


def test_release_removes_when_success(
    manager: WorktreeManager, fake_git: FakeGit
) -> None:
    handle = manager.acquire("trace-ok")
    fake_git.calls.clear()
    removed = manager.release(handle, success=True)
    assert removed is True
    assert fake_git.calls
    argv, _ = fake_git.calls[-1]
    assert argv[:2] == ["worktree", "remove"]


def test_release_handles_git_failure_without_raising(
    manager: WorktreeManager, fake_git: FakeGit
) -> None:
    handle = manager.acquire("trace-err")
    fake_git.calls.clear()
    fake_git.fail_next()
    removed = manager.release(handle, success=True)
    assert removed is False


# ---------------------------------------------------------------------------
# list_stale
# ---------------------------------------------------------------------------


def test_list_stale_returns_old_dirs(
    manager: WorktreeManager, fake_git: FakeGit, tmp_path: Path
) -> None:
    import os

    handle = manager.acquire("trace-old")
    # Reach into the filesystem to age the directory.
    age_target = handle.path
    past = 1  # 1970-01-01 — very stale.
    os.utime(age_target, (past, past))
    stale = manager.list_stale(older_than_seconds=60)
    assert age_target in stale


def test_list_stale_empty_when_no_base_dir(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    mgr = WorktreeManager(tmp_path, git=lambda *a, **k: None)  # type: ignore[arg-type]
    assert mgr.list_stale() == []


# ---------------------------------------------------------------------------
# WorktreeHandle
# ---------------------------------------------------------------------------


def test_handle_is_fallback_compares_resolved_paths(
    tmp_path: Path,
) -> None:
    """L1 fix — meaningful assertion: ``tmp_path`` and
    ``tmp_path/./..`` are the SAME directory after ``resolve()`` only
    when ``tmp_path`` is itself a root-level tmp dir. Instead we
    construct a genuinely non-normalised path
    (``tmp_path/child/..``) that ``resolve()`` collapses back to
    ``tmp_path`` — that case MUST report fallback=True."""
    child = tmp_path / "child"
    child.mkdir()
    noncanonical = tmp_path / "child" / ".."
    h = WorktreeHandle(
        path=tmp_path,
        branch="main",
        child_trace_id="x",
        base_branch="main",
        created_at=0,
        repo_root=noncanonical,
    )
    assert h.is_fallback is True, (
        "non-canonical repo_root should collapse to the same dir "
        "after resolve()"
    )
    # Explicit equal-path case:
    h2 = WorktreeHandle(
        path=tmp_path,
        branch="main",
        child_trace_id="x",
        base_branch="main",
        created_at=0,
        repo_root=tmp_path,
    )
    assert h2.is_fallback is True


def test_handle_non_fallback_when_different_dir(
    tmp_path: Path,
) -> None:
    wt = tmp_path / ".worktrees" / "y"
    wt.mkdir(parents=True)
    h = WorktreeHandle(
        path=wt,
        branch="agent/y",
        child_trace_id="y",
        base_branch="main",
        created_at=0,
        repo_root=tmp_path,
    )
    assert h.is_fallback is False
