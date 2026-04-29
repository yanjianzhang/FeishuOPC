"""B-3 git-worktree isolation for concurrent code-writing dispatches.

Each child agent that declares ``needs_worktree: true`` in its role
frontmatter gets a dedicated worktree at
``{repo_root}/.worktrees/{child_trace_id}`` on branch
``agent/{child_trace_id}``. Because a worktree is a fully-realised
checkout with its own index and HEAD, two children can
``git commit`` in parallel without contending on the main working
copy's ``repo_filelock``. The TL process — which owns the main
working copy — performs the final merge serially.

The manager deliberately degrades rather than fails:

- If ``git worktree add`` fails (concurrent creation, disk full,
  unsupported git version, …) the manager returns a *fallback*
  handle pointing at the main repo root. Callers that must
  distinguish fallback-vs-isolated can inspect
  :attr:`WorktreeHandle.is_fallback`; tools that accept
  ``working_dir`` treat both identically. This keeps B-3 a
  pure performance win — any failure mode reverts to the
  pre-B-3 behaviour.
- ``release`` with ``keep_on_failure=True`` retains the worktree
  on a failed child session so an operator can inspect the
  state. The stale-cleanup script handles long-term disk hygiene.

Concurrency invariants:

1. ``acquire`` for *different* ``child_trace_id`` values is safe to
   call concurrently — the underlying ``git worktree add`` call is
   wrapped in ``repo_filelock`` so only the metadata write serialises;
   the branch/directory names are already unique per-child.
2. ``acquire`` is idempotent for the *same* ``child_trace_id`` and
   same directory — a second call returns a matching handle without
   invoking git.
3. The manager never touches the main working copy's HEAD, index, or
   files. All mutations live under ``.git/worktrees/<id>`` and
   ``.worktrees/<id>``.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from feishu_agent.tools.git_sync_preflight import _repo_filelock

logger = logging.getLogger(__name__)

# A callable with the same surface as ``subprocess.run`` but narrowed
# to the shape we actually use. Tests pass a fake runner that records
# invocations and simulates failures.
GitRunner = Callable[[list[str], Path], subprocess.CompletedProcess]


def _default_git_runner(
    argv: list[str], cwd: Path
) -> subprocess.CompletedProcess:
    """Default git runner. Raises on non-zero exit so the manager's
    ``except CalledProcessError`` path is reachable without special
    handling for ``check=False``."""
    return subprocess.run(
        ["git", *argv],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


@dataclass(frozen=True)
class WorktreeHandle:
    """Immutable pointer to an acquired (or fallback) worktree.

    The pointer carries enough context for downstream code
    (bundles, TL merge step, audit) to make decisions without
    re-consulting the manager.
    """

    path: Path
    branch: str
    child_trace_id: str
    base_branch: str
    created_at: int
    repo_root: Path = field(default_factory=lambda: Path("."))

    @property
    def is_fallback(self) -> bool:
        """A fallback handle reuses the main repo root instead of a
        dedicated worktree. Tools accept both shapes; only merge-back
        logic and audit need to distinguish them."""
        return self.path.resolve() == self.repo_root.resolve()


class WorktreeManager:
    """Thin wrapper around ``git worktree`` with fallback + audit.

    The manager is intentionally stateless beyond its config —
    re-creating one every call is cheap, and multiple managers
    pointing at the same repo cooperate correctly because the
    serialising primitive is the on-disk ``.git`` filelock, not
    an instance lock.
    """

    def __init__(
        self,
        repo_root: Path,
        *,
        git: GitRunner | None = None,
        enabled: bool = True,
    ) -> None:
        # L2 fix — resolve symlinks once so ``WorktreeHandle.is_fallback``
        # (which compares ``resolve()``-normalised paths on both sides)
        # doesn't get confused when the caller passes a path containing
        # a symlink component.
        self._repo_root = repo_root.resolve() if repo_root.exists() else repo_root
        self._git = git or _default_git_runner
        self._enabled = enabled
        self._base_dir = self._repo_root / ".worktrees"

    # ------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------

    def acquire(
        self,
        child_trace_id: str,
        base_branch: str = "main",
    ) -> WorktreeHandle:
        """Create or attach to a worktree for ``child_trace_id``.

        Always returns a handle. Falls back to a handle pointing
        at the main repo root on any failure, logging at
        ``WARNING`` so the operator still sees the degradation.
        """
        if not child_trace_id:
            # Defensive: an empty id would create ``.worktrees/`` as
            # the worktree path, mangling the main dir. Treat as a
            # bug but don't crash — fall back silently.
            logger.warning(
                "WorktreeManager.acquire called with empty "
                "child_trace_id; using fallback"
            )
            return self._fallback(child_trace_id, base_branch)

        if not self._enabled:
            return self._fallback(child_trace_id, base_branch)

        wt_path = self._base_dir / child_trace_id
        branch = f"agent/{child_trace_id}"

        if wt_path.exists():
            # Idempotent re-acquire. We don't re-verify the branch
            # because a concurrent caller may have it checked out
            # exclusively; the caller gets a handle and proceeds.
            return WorktreeHandle(
                path=wt_path,
                branch=branch,
                child_trace_id=child_trace_id,
                base_branch=base_branch,
                created_at=int(time.time()),
                repo_root=self._repo_root,
            )

        self._base_dir.mkdir(parents=True, exist_ok=True)

        try:
            with _repo_filelock(self._repo_root):
                self._git(
                    [
                        "worktree",
                        "add",
                        "-b",
                        branch,
                        str(wt_path),
                        base_branch,
                    ],
                    self._repo_root,
                )
        except (subprocess.CalledProcessError, OSError) as exc:
            logger.warning(
                "git worktree add failed for %s (base=%s): %s; "
                "falling back to main working copy",
                child_trace_id,
                base_branch,
                exc,
            )
            return self._fallback(child_trace_id, base_branch)

        return WorktreeHandle(
            path=wt_path,
            branch=branch,
            child_trace_id=child_trace_id,
            base_branch=base_branch,
            created_at=int(time.time()),
            repo_root=self._repo_root,
        )

    def release(
        self,
        handle: WorktreeHandle,
        *,
        keep_on_failure: bool = True,
        success: bool = True,
    ) -> bool:
        """Remove the worktree referenced by ``handle``.

        Returns ``True`` when the worktree was actually removed,
        ``False`` when it was kept (fallback handle, keep-on-failure,
        or the git call failed). The caller can use this to decide
        whether to emit ``worktree.release`` audit events.
        """
        if handle.is_fallback:
            return False
        if not success and keep_on_failure:
            logger.info(
                "WorktreeManager: retaining worktree for post-mortem "
                "(path=%s, branch=%s)",
                handle.path,
                handle.branch,
            )
            return False
        try:
            with _repo_filelock(self._repo_root):
                self._git(
                    [
                        "worktree",
                        "remove",
                        "--force",
                        str(handle.path),
                    ],
                    self._repo_root,
                )
        except (subprocess.CalledProcessError, OSError) as exc:
            logger.warning(
                "git worktree remove failed for %s: %s", handle.path, exc
            )
            return False
        return True

    def list_stale(self, older_than_seconds: int = 86400) -> list[Path]:
        """Return worktree directories older than
        ``older_than_seconds``. Used by the cleanup script; the
        manager itself never auto-removes."""
        if not self._base_dir.exists():
            return []
        now = time.time()
        stale: list[Path] = []
        for p in self._base_dir.iterdir():
            if not p.is_dir():
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if now - mtime > older_than_seconds:
                stale.append(p)
        return stale

    # ------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------

    def _fallback(
        self, child_trace_id: str, base_branch: str
    ) -> WorktreeHandle:
        """Produce a handle pointing at the main repo root. All
        isolation is forfeit but the caller can proceed unchanged;
        callers that care about parallelism should check
        :attr:`WorktreeHandle.is_fallback` and either serialise on
        the main ``repo_filelock`` or degrade gracefully."""
        return WorktreeHandle(
            path=self._repo_root,
            branch=base_branch,
            child_trace_id=child_trace_id,
            base_branch=base_branch,
            created_at=int(time.time()),
            repo_root=self._repo_root,
        )
