"""Explicit-paths-only commit + push for doc / spec / review artifacts.

Why this exists
---------------
``GitOpsService`` is the right commit/push surface for code (it is
tied to ``PrePushInspector``, ``CodeWritePolicy.protected_branches``,
and fresh inspection tokens). But the Feishu PM bot never writes code
— it writes markdown (PRDs, specs, research notes, briefs) through
``WorkflowService.write_artifact``. Forcing those writes through
``GitOpsService`` has two wrong properties:

1. ``GitOpsService`` **refuses to push to main/master** by design
   (``protected_branches`` gate). But doc drive-by commits (a new
   idea, a backlog note, a vocabulary research note) genuinely
   belong on main — there is no "feature" to merge.
2. ``GitOpsService.commit`` uses ``git add -u`` / policy-compliant
   staging, which is the right behavior for a multi-file code edit
   session but the wrong behavior for a PM bot that wrote exactly
   one or two files and wants to publish ONLY those.

``ArtifactPublishService`` is the narrow counterpart for docs:

- Caller passes the **explicit** list of ``relative_paths`` to publish.
- Each path is validated against a per-agent allow-list of safe
  roots (``specs/``, ``stories/``, ``reviews/``, ``project_knowledge/``,
  ``docs/``).
- After ``git add -- <paths>``, we ``git diff --cached --name-only``
  and require the staged set to match the requested set exactly — any
  surprise additions (pre-commit hook auto-stages, stray
  ``_bmad/core/xxx`` write) abort with ``EXTRA_STAGED_FILES``.
- Commit runs normally (no ``--no-verify``; hooks stay in play).
- Push goes to the **current branch** — if PM is on main after
  preflight, commits land on main; if PM just created a feature
  branch via ``create-new-feature.sh``, commits land there. **No
  force push, ever.**
- The whole flow runs under the same ``repo_filelock`` the preflight
  uses, so a PM publish cannot collide with a concurrent TL preflight
  on the same shared clone.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from feishu_agent.tools.git_sync_preflight import repo_filelock

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ArtifactPublishError(Exception):
    """Base class. Always carries a stable ``code`` for LLM branching."""

    code: str = "ARTIFACT_PUBLISH_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnknownProjectError(ArtifactPublishError):
    code = "UNKNOWN_PROJECT"


class AgentNotAllowedError(ArtifactPublishError):
    code = "AGENT_NOT_ALLOWED"


class PathRejectedError(ArtifactPublishError):
    """Path failed root / traversal / sensitive-segment validation."""

    code = "PATH_REJECTED"


class PathMissingError(ArtifactPublishError):
    """Path validated OK but is not a file on disk — usually means the
    caller forgot to ``write_workflow_artifact`` before publishing."""

    code = "PATH_NOT_FOUND"


class NothingToCommitError(ArtifactPublishError):
    code = "NOTHING_TO_COMMIT"


class ExtraStagedFilesError(ArtifactPublishError):
    """Detected staged files we did not request. Usually a pre-commit
    hook auto-formatted something, or a previous write went unnoticed.
    We refuse to proceed rather than publish a surprise."""

    code = "EXTRA_STAGED_FILES"


class DetachedHeadError(ArtifactPublishError):
    code = "DETACHED_HEAD"


class CommitFailedError(ArtifactPublishError):
    code = "COMMIT_FAILED"


class PushFailedError(ArtifactPublishError):
    code = "PUSH_FAILED"


class CommitMessageRejectedError(ArtifactPublishError):
    code = "COMMIT_MESSAGE_REJECTED"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


# Per-agent allow-list of top-level directory roots under the project
# repo. A path is accepted when its FIRST path segment matches one of
# these values. Anything outside this list — ``lib/``, ``src/``,
# ``scripts/``, the root-level dotfiles — is rejected without even
# touching git. Code-authoring roots belong to ``GitOpsService``; keep
# them out of this table.
_AGENT_ALLOWED_PUBLISH_ROOTS: dict[str, frozenset[str]] = {
    "product_manager": frozenset(
        {
            "specs",
            "stories",
            "reviews",
            "project_knowledge",
            "docs",
            "briefs",
        }
    ),
    # Tech lead is not wired to this service (they still publish code
    # via GitOpsService). We deliberately leave ``tech_lead`` off the
    # table so that, if someone tries to enable it later without
    # reading this comment, they get a loud AGENT_NOT_ALLOWED error.
}


# Segments that must never appear anywhere in a published path. Mirrors
# ``code_write_service._HARDCODED_DENIED_SEGMENTS`` intent: ``.env``
# and friends are never publishable, regardless of agent or root.
_DENIED_PATH_SEGMENTS: frozenset[str] = frozenset(
    {
        ".env",
        ".env.local",
        ".git",
        ".larkagent",
        "secrets",
        "credentials.json",
        "node_modules",
    }
)


_COMMIT_MESSAGE_MAX_BYTES = 4 * 1024
_DEFAULT_GIT_TIMEOUT_SECONDS = 60
# We never append ``--force`` / ``--force-with-lease`` to push. The
# allow-list is defensive: if someone accidentally appends a custom
# flag later it has to be added here explicitly.
_ALLOWED_PUSH_FLAGS: frozenset[str] = frozenset()


@dataclass(frozen=True)
class PublishResult:
    project_id: str
    agent_name: str
    branch: str
    commit_sha: str
    commit_message: str
    paths: tuple[str, ...]
    remote: str
    pushed: bool
    push_output: str = ""
    elapsed_ms: int = 0
    skipped_reason: str | None = None  # reserved for dry-run / no-remote futures


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ArtifactPublishService:
    """Commit + push an explicit list of markdown artifacts.

    Safe to share across bot threads; holds no per-request state.
    """

    def __init__(
        self,
        *,
        project_roots: Mapping[str, Path],
        git_binary: str = "git",
        default_remote: str = "origin",
        timeout_seconds: int = _DEFAULT_GIT_TIMEOUT_SECONDS,
    ) -> None:
        self._project_roots: dict[str, Path] = {
            pid: root.resolve() for pid, root in project_roots.items()
        }
        self._git = git_binary
        self._default_remote = default_remote
        self._timeout_seconds = timeout_seconds

    # -- discovery ------------------------------------------------------

    def allowed_roots_for_agent(self, agent_name: str) -> tuple[str, ...]:
        return tuple(sorted(_AGENT_ALLOWED_PUBLISH_ROOTS.get(agent_name, frozenset())))

    def is_agent_enabled(self, agent_name: str) -> bool:
        return agent_name in _AGENT_ALLOWED_PUBLISH_ROOTS

    # -- publish --------------------------------------------------------

    def publish(
        self,
        *,
        agent_name: str,
        project_id: str,
        relative_paths: list[str],
        commit_message: str,
        remote: str | None = None,
    ) -> PublishResult:
        self._validate_commit_message(commit_message)
        root = self._resolve_project(project_id)
        allowed_roots = _AGENT_ALLOWED_PUBLISH_ROOTS.get(agent_name)
        if not allowed_roots:
            raise AgentNotAllowedError(
                f"Agent {agent_name!r} has no artifact-publish allowlist. "
                f"Known agents: {sorted(_AGENT_ALLOWED_PUBLISH_ROOTS)}"
            )

        normalized = self._validate_paths(root, relative_paths, allowed_roots)
        start = time.monotonic()
        remote = (remote or self._default_remote).strip()
        if not remote:
            raise ArtifactPublishError("remote must be non-empty.")

        with repo_filelock(root):
            branch = self._current_branch(root)
            if branch == "HEAD":
                raise DetachedHeadError(
                    "Refusing to publish in detached HEAD state. "
                    "Check out a real branch first."
                )

            self._git_run(root, ["add", "--", *normalized])
            staged = self._list_staged(root)
            requested_set = {p for p in normalized}
            extras = sorted(staged - requested_set)
            if extras:
                # Reset our adds so the worktree isn't left half-staged
                # on the way out. Best-effort: if reset fails we still
                # raise — the caller will see the git output anyway.
                self._safe_reset_paths(root, normalized)
                raise ExtraStagedFilesError(
                    "git index contains files beyond the requested set: "
                    f"{extras}. Either pre-commit hooks auto-staged them "
                    "or a prior write was left uncommitted. Clean the "
                    "worktree and retry."
                )

            if not staged:
                raise NothingToCommitError(
                    "Requested paths have no diff vs HEAD — nothing to publish."
                )

            self._do_commit(root, commit_message)
            commit_sha = self._git_out(root, ["rev-parse", "HEAD"]).strip()

            push_output = self._do_push(root, remote=remote, branch=branch)

        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "artifact_publish: agent=%s project=%s branch=%s commit=%s "
            "files=%d remote=%s elapsed_ms=%d",
            agent_name,
            project_id,
            branch,
            commit_sha[:12],
            len(normalized),
            remote,
            elapsed_ms,
        )
        return PublishResult(
            project_id=project_id,
            agent_name=agent_name,
            branch=branch,
            commit_sha=commit_sha,
            commit_message=commit_message,
            paths=tuple(normalized),
            remote=remote,
            pushed=True,
            push_output=push_output,
            elapsed_ms=elapsed_ms,
        )

    # -- guards ---------------------------------------------------------

    def _resolve_project(self, project_id: str) -> Path:
        root = self._project_roots.get(project_id)
        if root is None:
            raise UnknownProjectError(
                f"No project_root configured for project_id={project_id!r}. "
                f"Known: {sorted(self._project_roots)}"
            )
        return root

    def _validate_commit_message(self, message: str) -> None:
        if not isinstance(message, str):
            raise CommitMessageRejectedError("commit_message must be a string.")
        if not message.strip():
            raise CommitMessageRejectedError("commit_message must not be empty.")
        if len(message.encode("utf-8")) > _COMMIT_MESSAGE_MAX_BYTES:
            raise CommitMessageRejectedError(
                f"commit_message exceeds {_COMMIT_MESSAGE_MAX_BYTES} bytes."
            )
        # A full NUL byte breaks git's plumbing; reject it explicitly.
        if "\x00" in message:
            raise CommitMessageRejectedError("commit_message contains NUL byte.")

    def _validate_paths(
        self,
        root: Path,
        relative_paths: list[str],
        allowed_roots: frozenset[str],
    ) -> list[str]:
        if not isinstance(relative_paths, list) or not relative_paths:
            raise PathRejectedError("relative_paths must be a non-empty list.")
        if len(relative_paths) > 50:
            raise PathRejectedError("Too many paths (max 50 per publish).")

        seen: set[str] = set()
        normalized: list[str] = []
        for idx, raw in enumerate(relative_paths):
            if not isinstance(raw, str) or not raw:
                raise PathRejectedError(
                    f"relative_paths[{idx}] must be a non-empty string."
                )
            if raw.startswith("/"):
                raise PathRejectedError(
                    f"relative_paths[{idx}] must not be absolute: {raw!r}"
                )
            if ".." in raw.split("/"):
                raise PathRejectedError(
                    f"relative_paths[{idx}] contains '..': {raw!r}"
                )
            if "\\" in raw:
                raise PathRejectedError(
                    f"relative_paths[{idx}] must use forward slashes: {raw!r}"
                )
            # Canonicalize using POSIX separator; catches "specs//foo"
            # and bare "." segments.
            parts = [p for p in raw.split("/") if p not in ("", ".")]
            if not parts:
                raise PathRejectedError(
                    f"relative_paths[{idx}] is empty after normalization."
                )
            if parts[0] not in allowed_roots:
                raise PathRejectedError(
                    f"relative_paths[{idx}] root {parts[0]!r} not in "
                    f"allow-list {sorted(allowed_roots)}."
                )
            for seg in parts:
                if seg in _DENIED_PATH_SEGMENTS:
                    raise PathRejectedError(
                        f"relative_paths[{idx}] contains denied segment "
                        f"{seg!r}: {raw!r}"
                    )
            canonical = "/".join(parts)
            if canonical in seen:
                raise PathRejectedError(
                    f"relative_paths[{idx}] duplicates a prior entry: {canonical}"
                )
            seen.add(canonical)

            abs_target = (root / canonical).resolve()
            # resolved path MUST stay inside project_root — defends
            # against symlink escape after the string check.
            try:
                abs_target.relative_to(root)
            except ValueError as exc:
                raise PathRejectedError(
                    f"relative_paths[{idx}] escapes project root: {raw!r}"
                ) from exc
            if not abs_target.is_file():
                raise PathMissingError(
                    f"relative_paths[{idx}] is not a file on disk: {canonical}"
                )
            normalized.append(canonical)
        return normalized

    # -- git plumbing ---------------------------------------------------

    def _current_branch(self, root: Path) -> str:
        return self._git_out(root, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()

    def _list_staged(self, root: Path) -> set[str]:
        raw = self._git_out(root, ["diff", "--cached", "--name-only"])
        return {line.strip() for line in raw.splitlines() if line.strip()}

    def _safe_reset_paths(self, root: Path, paths: list[str]) -> None:
        try:
            self._git_run(root, ["reset", "HEAD", "--", *paths])
        except Exception:  # pragma: no cover - best effort cleanup
            logger.warning(
                "artifact_publish: failed to reset paths after extra-staged "
                "rejection (paths=%s)",
                paths,
                exc_info=True,
            )

    def _do_commit(self, root: Path, message: str) -> None:
        try:
            subprocess.run(
                [self._git, "commit", "-m", message],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=True,
                timeout=self._timeout_seconds,
            )
        except subprocess.CalledProcessError as exc:
            raise CommitFailedError(
                f"git commit failed (exit={exc.returncode}): "
                f"{(exc.stderr or exc.stdout or '').strip()[:500]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CommitFailedError(
                f"git commit timed out after {self._timeout_seconds}s."
            ) from exc

    def _do_push(self, root: Path, *, remote: str, branch: str) -> str:
        argv = [self._git, "push", remote, branch]
        # Defensive tripwire: if someone edits the line above to allow
        # flags, this check catches anything not in the empty
        # allow-list. Keeps "no --force, ever" literally enforced at
        # the subprocess boundary.
        for flag in argv[2:]:
            if flag.startswith("-") and flag not in _ALLOWED_PUSH_FLAGS:
                raise PushFailedError(
                    f"Refusing to pass unreviewed flag to git push: {flag!r}"
                )
        try:
            proc = subprocess.run(
                argv,
                cwd=str(root),
                capture_output=True,
                text=True,
                check=True,
                timeout=self._timeout_seconds,
            )
        except subprocess.CalledProcessError as exc:
            raise PushFailedError(
                f"git push failed (exit={exc.returncode}): "
                f"{(exc.stderr or exc.stdout or '').strip()[:500]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise PushFailedError(
                f"git push timed out after {self._timeout_seconds}s."
            ) from exc
        return (proc.stderr or proc.stdout or "").strip()

    def _git_run(self, root: Path, args: list[str]) -> None:
        try:
            subprocess.run(
                [self._git, *args],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=True,
                timeout=self._timeout_seconds,
            )
        except subprocess.CalledProcessError as exc:
            raise ArtifactPublishError(
                f"git {args[0]} failed (exit={exc.returncode}): "
                f"{(exc.stderr or exc.stdout or '').strip()[:500]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ArtifactPublishError(
                f"git {args[0]} timed out after {self._timeout_seconds}s."
            ) from exc

    def _git_out(self, root: Path, args: list[str]) -> str:
        try:
            proc = subprocess.run(
                [self._git, *args],
                cwd=str(root),
                capture_output=True,
                text=True,
                check=True,
                timeout=self._timeout_seconds,
            )
        except subprocess.CalledProcessError as exc:
            raise ArtifactPublishError(
                f"git {args[0]} failed (exit={exc.returncode}): "
                f"{(exc.stderr or exc.stdout or '').strip()[:500]}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ArtifactPublishError(
                f"git {args[0]} timed out after {self._timeout_seconds}s."
            ) from exc
        return proc.stdout
