"""Branch-gated git commit + push for the tech-lead agent.

Tech-lead is the only role with code-write access, and also the only
role allowed to move a commit to the remote. Two invariants this
service enforces (neither is LLM-checkable; both are hardcoded):

1. **Branch gate** — never commits to, and **never pushes to**, a
   branch in ``policy.protected_branches`` (defaults include ``main``
   / ``master``; projects can extend via ``policies.jsonl``).
2. **Fresh inspection gate** — ``push_current_branch`` requires an
   ``inspection_token`` that:
   - was minted by ``PrePushInspector.inspect`` (i.e. inspection
     succeeded with zero blockers),
   - is still inside TTL (10 min by default),
   - is pinned to the HEAD SHA + branch at inspection time, so pushing
     after writing more code (without re-inspecting) is refused.

All git mutations flow through ``subprocess`` with a fixed timeout and
``check=False`` so we can surface precise error codes to the LLM
instead of a Python traceback. Every commit / push is appended to the
``CodeWriteAuditLog`` so you get one trace file per session with the
full write → commit → push history.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from feishu_agent.tools.code_write_service import (
    CodeWriteAuditLog,
    CodeWritePolicy,
)
from feishu_agent.tools.pre_push_inspector import PrePushInspector

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GitOpsError(Exception):
    """Base class for git-ops failures; always carries a stable ``code``."""

    code: str = "GIT_OPS_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class GitProjectError(GitOpsError):
    code = "GIT_OPS_UNKNOWN_PROJECT"


class GitBranchProtectedError(GitOpsError):
    code = "GIT_OPS_BRANCH_PROTECTED"


class GitInspectionRequiredError(GitOpsError):
    code = "GIT_OPS_INSPECTION_REQUIRED"


class GitCommandError(GitOpsError):
    code = "GIT_OPS_COMMAND_FAILED"


class GitNothingToCommitError(GitOpsError):
    code = "GIT_OPS_NOTHING_TO_COMMIT"


class GitSyncDirtyError(GitOpsError):
    code = "GIT_OPS_SYNC_DIRTY"


class GitSyncDivergedError(GitOpsError):
    code = "GIT_OPS_SYNC_DIVERGED"


class GitNoUpstreamError(GitOpsError):
    code = "GIT_OPS_NO_UPSTREAM"


class GitBranchExistsError(GitOpsError):
    code = "GIT_OPS_BRANCH_EXISTS"


class GitInvalidBranchSpecError(GitOpsError):
    """Raised when the caller asks to create a branch whose name is
    malformed (bad kind, bad slug, or protected/denylisted). Distinct
    from ``GitBranchProtectedError`` which is specifically about trying
    to commit/push onto a protected branch."""

    code = "GIT_OPS_INVALID_BRANCH_SPEC"


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class GitCommitResult:
    project_id: str
    branch: str
    commit_sha: str
    message: str
    files_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "message": self.message,
            "files_count": self.files_count,
        }


@dataclass
class GitPushResult:
    project_id: str
    branch: str
    remote: str
    pushed_head_sha: str
    output: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "branch": self.branch,
            "remote": self.remote,
            "pushed_head_sha": self.pushed_head_sha,
            "output": self.output,
        }


@dataclass
class StartWorkBranchResult:
    """Outcome of ``GitOpsService.start_work_branch``.

    - ``branch``: the newly-created branch name (e.g. ``feat/3-2-steal-api``).
    - ``base`` / ``base_upstream_sha``: which remote branch we forked from, at
      what SHA. Useful for the PR body and audit log.
    - ``discarded_dirty_paths``: populated (truncated to 10) only when the
      caller opted into ``allow_discard_dirty=True`` AND there were dirty
      paths; empty list otherwise.
    - ``previous_branch`` / ``previous_head_sha``: what we were on before the
      switch — so the tech-lead can tell the user "I left your old work on
      branch X at sha Y" if something was in progress.
    """

    project_id: str
    branch: str
    base: str
    remote: str
    head_sha: str
    base_upstream_sha: str
    previous_branch: str | None
    previous_head_sha: str | None
    discarded_dirty_paths: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "branch": self.branch,
            "base": self.base,
            "remote": self.remote,
            "head_sha": self.head_sha,
            "base_upstream_sha": self.base_upstream_sha,
            "previous_branch": self.previous_branch,
            "previous_head_sha": self.previous_head_sha,
            "discarded_dirty_paths": list(self.discarded_dirty_paths),
        }


@dataclass
class GitSyncResult:
    project_id: str
    branch: str
    remote: str
    status: str  # "up_to_date" | "fast_forwarded" | "ahead_no_action"
    ahead_count: int
    behind_count: int
    old_head_sha: str
    new_head_sha: str
    pulled_commits: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "branch": self.branch,
            "remote": self.remote,
            "status": self.status,
            "ahead_count": self.ahead_count,
            "behind_count": self.behind_count,
            "old_head_sha": self.old_head_sha,
            "new_head_sha": self.new_head_sha,
            "pulled_commits": list(self.pulled_commits),
        }


@dataclass
class ForceSyncResult:
    """Outcome of ``GitOpsService.force_sync_to_remote``.

    Destructive rewrite: ``target_branch`` is force-pointed at
    ``<remote>/<target_branch>``, any uncommitted tracked edits and
    untracked files on that worktree are gone. ``previous_head_sha``
    is the only handle back into the discarded state via reflog, so
    we always return it even when the caller doesn't ask for it.
    """

    project_id: str
    branch: str
    remote: str
    previous_branch: str
    previous_head_sha: str
    new_head_sha: str
    cleaned_paths_count: int
    cleaned_paths_preview: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "branch": self.branch,
            "remote": self.remote,
            "previous_branch": self.previous_branch,
            "previous_head_sha": self.previous_head_sha,
            "new_head_sha": self.new_head_sha,
            "cleaned_paths_count": self.cleaned_paths_count,
            "cleaned_paths_preview": list(self.cleaned_paths_preview),
        }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class GitOpsService:
    COMMIT_MESSAGE_MAX_BYTES = 4 * 1024
    TIMEOUT_SECONDS = 60

    def __init__(
        self,
        *,
        project_roots: dict[str, Path],
        policies: dict[str, CodeWritePolicy],
        inspector: PrePushInspector,
        audit_log: CodeWriteAuditLog | None = None,
        git_binary: str = "git",
        default_remote: str = "origin",
    ) -> None:
        self._project_roots = {k: Path(v) for k, v in project_roots.items()}
        self._policies = dict(policies)
        self._inspector = inspector
        self._audit = audit_log
        self._git = git_binary
        self._default_remote = default_remote

    # -- public API -----------------------------------------------------

    def commit(
        self,
        *,
        project_id: str,
        message: str,
    ) -> GitCommitResult:
        root, pol = self._resolve(project_id)
        self._validate_message(message)

        branch = self._current_branch(root)
        if pol.is_protected_branch(branch):
            raise GitBranchProtectedError(
                f"Refusing to commit on protected branch {branch!r}. "
                f"Switch to a feature branch first."
            )

        # Stage all tracked changes + new files already under allowed paths.
        # We deliberately do NOT run `git add -A`; that would sweep in files
        # PrePushInspector flagged as path violations. Instead we stage only
        # files that already appear in `git status --porcelain`, which
        # agent-controlled writes produced.
        # Simpler + safe for our use: `git add -u` (tracked changes) + then
        # explicit add of untracked files the caller vetted. We keep it
        # minimal: `git add -A` on changes discovered by `git status`
        # filtered against the policy. If the filtering drops everything,
        # we raise GitNothingToCommitError.
        staged_count = self._stage_policy_compliant_changes(root, pol)
        if staged_count == 0:
            raise GitNothingToCommitError(
                "Nothing to commit (no policy-compliant staged changes)."
            )

        self._git_run(
            root,
            [
                "commit",
                "-m",
                message,
            ],
        )
        commit_sha = self._git_out(root, ["rev-parse", "HEAD"]).strip()

        self._audit_append(
            {
                "event": "git_commit",
                "project_id": project_id,
                "branch": branch,
                "commit_sha": commit_sha,
                "message": message,
                "files_count": staged_count,
            }
        )
        return GitCommitResult(
            project_id=project_id,
            branch=branch,
            commit_sha=commit_sha,
            message=message,
            files_count=staged_count,
        )

    def push_current_branch(
        self,
        *,
        project_id: str,
        inspection_token: str,
        remote: str | None = None,
    ) -> GitPushResult:
        root, pol = self._resolve(project_id)
        remote = (remote or self._default_remote).strip()
        if not remote:
            raise GitOpsError("remote must be non-empty.")

        branch = self._current_branch(root)
        if pol.is_protected_branch(branch):
            raise GitBranchProtectedError(
                f"Refusing to push to protected branch {branch!r}. "
                f"Push feature branches only; the human ops owner moves "
                f"commits onto main/master."
            )

        head_sha = self._git_out(root, ["rev-parse", "HEAD"]).strip()
        if not self._inspector.consume_token(
            inspection_token,
            expected_head_sha=head_sha,
            expected_branch=branch,
        ):
            raise GitInspectionRequiredError(
                "inspection_token missing / expired / mismatched. "
                "Call run_pre_push_inspection again; if it still shows "
                "ok=true, retry push with the returned inspection_token."
            )

        output = self._git_out(
            root,
            ["push", remote, branch],
            capture_stderr=True,
        )

        self._audit_append(
            {
                "event": "git_push",
                "project_id": project_id,
                "branch": branch,
                "remote": remote,
                "head_sha": head_sha,
            }
        )
        return GitPushResult(
            project_id=project_id,
            branch=branch,
            remote=remote,
            pushed_head_sha=head_sha,
            output=output.strip(),
        )

    # Work-branch prefixes the tech-lead is allowed to open. Chosen to
    # match the conventional-commit-ish vocabulary already used in our
    # PR titles so tools like `gh pr list --label` can key off the
    # prefix. "exp" is deliberately included so spike / debug branches
    # are first-class instead of being squeezed into "feat/".
    _ALLOWED_BRANCH_KINDS: frozenset[str] = frozenset(
        {"feat", "fix", "debug", "chore", "docs", "refactor", "test", "exp"}
    )

    # Slug must start with an alnum and contain only kebab-case-safe
    # characters. Upper cap of 80 chars keeps the full ref under git's
    # 250-char soft limit with room for a long ``kind`` prefix.
    _SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")

    def start_work_branch(
        self,
        *,
        project_id: str,
        kind: str,
        slug: str,
        base_branch: str = "main",
        remote: str | None = None,
        allow_discard_dirty: bool = False,
    ) -> StartWorkBranchResult:
        """Create a fresh work branch off ``remote/base_branch``.

        Pipeline (all 5 steps must succeed or we raise and leave the
        worktree unchanged except for step 2's discard, which is
        explicitly opt-in):

        1. **Validate inputs**: ``kind`` ∈ allow-list, ``slug`` matches
           the kebab-case regex, the resulting branch name is not a
           protected branch per ``policy.protected_branches``, and the
           branch doesn't already exist locally (both failure modes raise
           typed errors so the LLM can pick a different slug).
        2. **Dirty-tree gate**: if ``git status --porcelain`` has entries:
           - ``allow_discard_dirty=False`` (default) → raise
             ``GitSyncDirtyError``; the caller decides.
           - ``allow_discard_dirty=True`` → ``git reset --hard HEAD`` +
             ``git clean -fd`` to nuke **uncommitted** changes. Note:
             unpushed commits on the current branch are left alone —
             they stay on that branch and are recoverable via
             ``git branch`` / ``git reflog``.
        3. **Fetch base**: ``git fetch <remote> <base_branch>``. Verify
           ``<remote>/<base_branch>`` exists locally after fetch;
           otherwise raise ``GitNoUpstreamError``.
        4. **Branch off the remote tip**: ``git checkout -b <kind>/<slug>
           <remote>/<base_branch>``. Local ``main`` (if any) is NOT
           touched — we fork directly from the remote ref, so user's
           local ``main`` keeps whatever state they had.
        5. **Audit**: append a ``start_work_branch`` record to the audit
           log so the lineage (prev branch + prev head → new branch +
           new head) is reconstructible.

        Design note: this is how we fix the "all stories keep piling
        onto one accumulator branch" bug. The tech-lead calls this
        before every new piece of work and gets a clean branch per
        story / fix / debug session.
        """
        root, pol = self._resolve(project_id)
        remote = (remote or self._default_remote).strip()
        if not remote:
            raise GitOpsError("remote must be non-empty.")

        # -- 1. validate inputs --------------------------------------
        if kind not in self._ALLOWED_BRANCH_KINDS:
            raise GitInvalidBranchSpecError(
                f"kind={kind!r} not allowed; pick one of "
                f"{sorted(self._ALLOWED_BRANCH_KINDS)}."
            )
        if not self._SLUG_RE.fullmatch(slug):
            raise GitInvalidBranchSpecError(
                f"slug={slug!r} must match [a-z0-9][a-z0-9._-]{{0,79}} "
                f"(kebab-case, alnum-first, max 80 chars)."
            )
        target_branch = f"{kind}/{slug}"
        if pol.is_protected_branch(target_branch):
            raise GitBranchProtectedError(
                f"Refusing to create work branch {target_branch!r}: "
                f"it matches policy.protected_branches."
            )

        try:
            self._git_out(root, ["rev-parse", "--verify", f"refs/heads/{target_branch}"])
            branch_exists = True
        except GitCommandError:
            branch_exists = False
        if branch_exists:
            raise GitBranchExistsError(
                f"Branch {target_branch!r} already exists locally. "
                f"Pick a different slug, or delete the existing branch first."
            )

        # -- Capture "before" state for the result record -----------
        try:
            prev_branch: str | None = self._current_branch(root)
        except GitBranchProtectedError:
            # Detached HEAD — not fatal for branch creation, just note.
            prev_branch = None
        try:
            prev_head: str | None = self._git_out(root, ["rev-parse", "HEAD"]).strip()
        except GitCommandError:
            prev_head = None

        # -- 2. dirty-tree gate --------------------------------------
        porcelain = self._git_out(root, ["status", "--porcelain=v1"]).strip()
        discarded: list[str] = []
        if porcelain:
            lines = porcelain.splitlines()
            if not allow_discard_dirty:
                preview = [line[3:].split(" -> ")[-1] for line in lines[:5]]
                raise GitSyncDirtyError(
                    f"Worktree has {len(lines)} uncommitted change(s); refusing "
                    f"to switch branches and lose them. Commit / stash first, "
                    f"or retry with allow_discard_dirty=True to discard. "
                    f"Paths: {preview}"
                    + (" ..." if len(lines) > 5 else "")
                )
            # User explicitly asked to discard. Capture a preview of what
            # we threw away so it shows up in the audit log and the
            # Feishu thread.
            discarded = [
                line[3:].split(" -> ")[-1] for line in lines[:10]
            ]
            self._git_run(root, ["reset", "--hard", "HEAD"])
            self._git_run(root, ["clean", "-fd"])

        # -- 3. fetch base ------------------------------------------
        try:
            self._git_run(root, ["fetch", remote, base_branch])
        except GitCommandError as exc:
            if "couldn't find remote ref" in str(exc):
                raise GitNoUpstreamError(
                    f"Remote {remote!r} has no branch {base_branch!r} — "
                    f"can't base work off it. Check the base branch name."
                ) from exc
            raise

        upstream_ref = f"{remote}/{base_branch}"
        try:
            base_sha = self._git_out(
                root, ["rev-parse", "--verify", upstream_ref]
            ).strip()
        except GitCommandError as exc:
            raise GitNoUpstreamError(
                f"{upstream_ref} not present after fetch."
            ) from exc

        # -- 4. create + check out new branch at the remote tip -----
        self._git_run(
            root, ["checkout", "-b", target_branch, upstream_ref]
        )
        new_head = self._git_out(root, ["rev-parse", "HEAD"]).strip()

        # -- 5. audit -----------------------------------------------
        self._audit_append(
            {
                "event": "start_work_branch",
                "project_id": project_id,
                "branch": target_branch,
                "base": base_branch,
                "base_upstream_sha": base_sha,
                "new_head_sha": new_head,
                "previous_branch": prev_branch,
                "previous_head_sha": prev_head,
                "discarded_dirty_count": len(discarded),
            }
        )

        return StartWorkBranchResult(
            project_id=project_id,
            branch=target_branch,
            base=base_branch,
            remote=remote,
            head_sha=new_head,
            base_upstream_sha=base_sha,
            previous_branch=prev_branch,
            previous_head_sha=prev_head,
            discarded_dirty_paths=discarded,
        )

    def sync_with_remote(
        self,
        *,
        project_id: str,
        remote: str | None = None,
    ) -> GitSyncResult:
        """Fetch ``remote`` and fast-forward the current branch if safe.

        Safe = worktree clean AND local has not diverged from the upstream
        (i.e. we're strictly behind or up-to-date). On any of the unsafe
        cases we raise a typed error instead of mutating the worktree:

        - worktree dirty → ``GitSyncDirtyError``
        - local ahead AND behind → ``GitSyncDivergedError``
        - remote tracking branch missing → ``GitNoUpstreamError``

        Returns a ``GitSyncResult`` on success; does NOT push. Runs
        without requiring an inspection token — it's a read-then-pull,
        never a publish.
        """
        root, _pol = self._resolve(project_id)
        remote = (remote or self._default_remote).strip()
        if not remote:
            raise GitOpsError("remote must be non-empty.")

        branch = self._current_branch(root)

        # Always fetch first so divergence math is accurate.
        try:
            self._git_run(root, ["fetch", remote, branch])
        except GitCommandError as exc:
            # If the remote doesn't know this branch yet, `git fetch` may
            # fail or succeed-with-nothing depending on version. Treat
            # "fatal: couldn't find remote ref" as no-upstream.
            if "couldn't find remote ref" in str(exc):
                raise GitNoUpstreamError(
                    f"Remote {remote!r} has no branch {branch!r} — nothing to sync."
                ) from exc
            raise

        upstream_ref = f"{remote}/{branch}"
        # Check the upstream actually exists locally now.
        try:
            self._git_out(root, ["rev-parse", "--verify", upstream_ref])
        except GitCommandError as exc:
            raise GitNoUpstreamError(
                f"{upstream_ref} not present locally after fetch — "
                f"no upstream to sync against."
            ) from exc

        # Count divergence.
        counts_line = self._git_out(
            root,
            ["rev-list", "--left-right", "--count", f"HEAD...{upstream_ref}"],
        ).strip()
        parts = counts_line.split()
        ahead = int(parts[0]) if parts and parts[0].isdigit() else 0
        behind = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

        old_head = self._git_out(root, ["rev-parse", "HEAD"]).strip()

        if ahead == 0 and behind == 0:
            return GitSyncResult(
                project_id=project_id,
                branch=branch,
                remote=remote,
                status="up_to_date",
                ahead_count=0,
                behind_count=0,
                old_head_sha=old_head,
                new_head_sha=old_head,
                pulled_commits=[],
            )

        if ahead > 0 and behind > 0:
            raise GitSyncDivergedError(
                f"Local branch {branch!r} has diverged from {upstream_ref} "
                f"(ahead {ahead}, behind {behind}). Refusing auto-merge; "
                f"resolve manually (rebase / merge / reset)."
            )

        if ahead > 0 and behind == 0:
            # Nothing to pull; we're ahead. Not an error — just report.
            return GitSyncResult(
                project_id=project_id,
                branch=branch,
                remote=remote,
                status="ahead_no_action",
                ahead_count=ahead,
                behind_count=0,
                old_head_sha=old_head,
                new_head_sha=old_head,
                pulled_commits=[],
            )

        # ahead == 0, behind > 0 → safe FF candidate. Check worktree.
        porcelain = self._git_out(root, ["status", "--porcelain=v1"])
        if porcelain.strip():
            dirty_paths = [
                line[3:].split(" -> ")[-1]
                for line in porcelain.splitlines()
                if len(line) >= 4
            ]
            raise GitSyncDirtyError(
                f"Worktree has {len(dirty_paths)} uncommitted "
                f"change(s) — refusing to pull. "
                f"Paths: {dirty_paths[:5]}"
                + (" ..." if len(dirty_paths) > 5 else "")
            )

        self._git_run(root, ["pull", "--ff-only", remote, branch])
        new_head = self._git_out(root, ["rev-parse", "HEAD"]).strip()
        pulled_log = self._git_out(
            root, ["log", "--oneline", f"{old_head}..{new_head}"]
        ).strip().splitlines()

        self._audit_append(
            {
                "event": "git_sync",
                "project_id": project_id,
                "branch": branch,
                "remote": remote,
                "old_head": old_head,
                "new_head": new_head,
                "pulled_count": len(pulled_log),
            }
        )
        return GitSyncResult(
            project_id=project_id,
            branch=branch,
            remote=remote,
            status="fast_forwarded",
            ahead_count=0,
            behind_count=behind,
            old_head_sha=old_head,
            new_head_sha=new_head,
            pulled_commits=pulled_log,
        )

    def force_sync_to_remote(
        self,
        *,
        project_id: str,
        remote: str | None = None,
        target_branch: str = "main",
    ) -> ForceSyncResult:
        """Discard local state and force ``target_branch`` to ``<remote>/<target_branch>``.

        Pipeline:
          1. ``git fetch <remote> <target_branch>`` — refresh the remote ref.
          2. ``git reset --hard HEAD`` on the current branch — drop any
             uncommitted tracked edits so the following checkout can't
             fail on "would be overwritten" errors.
          3. ``git checkout -B <target_branch> <remote>/<target_branch>``
             — create-or-reset ``target_branch`` to point at the remote
             tip and check it out. Works whether or not the local
             branch already exists.
          4. ``git clean -fd`` — remove untracked files and directories
             in the worktree. Deliberately NOT ``-x``: files covered by
             ``.gitignore`` (``.venv``, ``node_modules``, build caches)
             stay put, because blowing those away costs the user a slow
             reinstall without fixing a real sync issue.

        Invoked exclusively through the preflight confirm flow — the
        LLM does not get a matching tool. Safety relies entirely on
        the human reply "确认" before this is called. We audit-log
        ``previous_head_sha`` before the rewrite so the ahead-commits
        are still findable via ``git reflog``.

        Holds ``repo_filelock`` for the whole pipeline so a concurrent
        preflight / artifact publish can't race on HEAD. Import is
        deferred to method body to avoid a circular import with
        ``git_sync_preflight``.
        """
        from feishu_agent.tools.git_sync_preflight import repo_filelock

        root, _pol = self._resolve(project_id)
        remote = (remote or self._default_remote).strip()
        if not remote:
            raise GitOpsError("remote must be non-empty.")
        target_branch = (target_branch or "").strip()
        if not target_branch:
            raise GitOpsError("target_branch must be non-empty.")

        with repo_filelock(root):
            # Capture pre-state BEFORE anything destructive. Both fields
            # are best-effort: a detached HEAD or a missing ref just
            # gets an empty placeholder so reflog lookups still work.
            try:
                previous_branch = self._current_branch(root)
            except GitBranchProtectedError:
                previous_branch = "HEAD"
            try:
                previous_head_sha = self._git_out(
                    root, ["rev-parse", "HEAD"]
                ).strip()
            except GitCommandError:
                previous_head_sha = ""

            try:
                self._git_run(root, ["fetch", remote, target_branch])
            except GitCommandError as exc:
                # NOTE (M-3): ``"couldn't find remote ref"`` is the
                # English phrasing; git locale overrides (e.g.
                # ``LC_MESSAGES=zh_CN``) will translate it and this
                # match will fall through. The subsequent
                # ``rev-parse --verify`` below is the locale-safe
                # backstop — it surfaces the same ``GitNoUpstreamError``
                # unconditionally if the ref isn't present after
                # fetch, regardless of which language git prints its
                # fetch error in.
                if "couldn't find remote ref" in str(exc):
                    raise GitNoUpstreamError(
                        f"Remote {remote!r} has no branch {target_branch!r} — "
                        f"can't force sync to it."
                    ) from exc
                raise

            upstream_ref = f"{remote}/{target_branch}"
            try:
                self._git_out(
                    root, ["rev-parse", "--verify", upstream_ref]
                )
            except GitCommandError as exc:
                raise GitNoUpstreamError(
                    f"{upstream_ref} not present after fetch."
                ) from exc

            # Step 2: drop uncommitted tracked edits on whatever branch
            # we're currently on. Without this, step 3's checkout can
            # fail with "your local changes would be overwritten".
            # Unpushed commits are NOT affected here — they live on the
            # branch ref, which is only moved in step 3.
            self._git_run(root, ["reset", "--hard", "HEAD"])

            # Step 3: -B creates or resets the branch AND checks it
            # out. Equivalent to ``checkout main; reset --hard origin/main``
            # but works in one step whether or not ``main`` already
            # exists locally (e.g. fresh clone that was shallow-checked
            # out on a different branch).
            self._git_run(
                root, ["checkout", "-B", target_branch, upstream_ref]
            )
            new_head = self._git_out(root, ["rev-parse", "HEAD"]).strip()

            # Step 4: preview what clean will nuke, then actually nuke.
            # Dry-run is cheap and lets us report a preview to the user
            # + feed the audit log with an accurate count.
            dry_run = self._git_out(root, ["clean", "-fd", "-n"])
            cleaned_lines = [
                ln for ln in dry_run.splitlines() if ln.strip()
            ]
            cleaned_paths_preview = [
                (
                    ln[len("Would remove ") :].strip()
                    if ln.startswith("Would remove ")
                    else ln.strip()
                )
                for ln in cleaned_lines[:10]
            ]
            cleaned_paths_count = len(cleaned_lines)
            if cleaned_paths_count:
                self._git_run(root, ["clean", "-fd"])

            self._audit_append(
                {
                    "event": "force_sync_to_remote",
                    "project_id": project_id,
                    "remote": remote,
                    "target_branch": target_branch,
                    "previous_branch": previous_branch,
                    "previous_head_sha": previous_head_sha,
                    "new_head_sha": new_head,
                    "cleaned_paths_count": cleaned_paths_count,
                }
            )

            return ForceSyncResult(
                project_id=project_id,
                branch=target_branch,
                remote=remote,
                previous_branch=previous_branch,
                previous_head_sha=previous_head_sha,
                new_head_sha=new_head,
                cleaned_paths_count=cleaned_paths_count,
                cleaned_paths_preview=cleaned_paths_preview,
            )

    # -- internal -------------------------------------------------------

    def _resolve(self, project_id: str) -> tuple[Path, CodeWritePolicy]:
        root = self._project_roots.get(project_id)
        pol = self._policies.get(project_id)
        if root is None or pol is None:
            raise GitProjectError(
                f"No git-ops config for project_id={project_id!r}."
            )
        if not (root / ".git").exists():
            raise GitCommandError(f"{root} is not a git checkout.")
        return root, pol

    def _validate_message(self, message: str) -> None:
        if not message or not message.strip():
            raise GitOpsError("commit message must be non-empty.")
        if len(message.encode("utf-8")) > self.COMMIT_MESSAGE_MAX_BYTES:
            raise GitOpsError(
                f"commit message too long: > {self.COMMIT_MESSAGE_MAX_BYTES} bytes."
            )

    def _current_branch(self, root: Path) -> str:
        branch = self._git_out(root, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
        if not branch or branch == "HEAD":
            raise GitBranchProtectedError(
                "Current HEAD is detached; refusing to commit or push. "
                "Check out a feature branch first."
            )
        return branch

    def _stage_policy_compliant_changes(
        self, root: Path, pol: CodeWritePolicy
    ) -> int:
        """Stage every modified / added / untracked path that is inside
        ``policy.allowed_write_roots`` OR ``policy.allowed_artifact_roots``
        (story / review / spec files). Paths outside both (which the
        pre-push inspector already flags as blockers) are NOT staged, so
        they can never end up in a commit authored by the agent.

        Uses ``git status --porcelain=v1 -z`` so NUL-separated records
        survive paths with spaces, quotes, or non-ASCII bytes unchanged.
        The older ``--porcelain=v1`` + ``splitlines()`` path mishandled
        renames with spaces (``R  "a b" -> "c d"``) because git escapes
        those when line-mode is on.
        """
        # --porcelain=v1 -z emits records separated by NUL. A record
        # begins with a 3-char prefix ("XY "), then the path, and for
        # rename/copy (R/C) records there is an EXTRA NUL followed by
        # the *original* path. We peek ahead one record for those.
        raw = self._git_out(root, ["status", "--porcelain=v1", "-z"])
        records = raw.split("\0")
        staged = 0
        i = 0
        while i < len(records):
            rec = records[i]
            i += 1
            if not rec:
                continue
            if len(rec) < 4:
                continue
            xy = rec[:2]
            path = rec[3:]
            # Rename / copy entries: next record is the old name; we
            # only care about the new one, but we must consume it so
            # indexes stay aligned.
            if xy and xy[0] in ("R", "C"):
                if i < len(records):
                    i += 1
            if not pol.is_commit_allowed_path(path):
                continue
            if pol.has_denied_segment(path):
                continue
            try:
                self._git_run(root, ["add", "--", path])
                staged += 1
            except GitCommandError:
                logger.warning(
                    "git add failed for %s; continuing", path, exc_info=True
                )
        return staged

    def _audit_append(self, record: dict[str, Any]) -> None:
        if self._audit is None:
            return
        record = {"ts": time.time(), **record}
        try:
            self._audit.append(record)
        except Exception:
            logger.warning("git-ops audit append failed", exc_info=True)

    def _git_out(
        self, root: Path, args: list[str], *, capture_stderr: bool = False
    ) -> str:
        try:
            res = subprocess.run(
                [self._git, *args],
                cwd=str(root),
                check=False,
                capture_output=True,
                text=True,
                timeout=self.TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise GitCommandError(f"git binary not found: {self._git}") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitCommandError(
                f"git {shlex.join(args)} timed out after {self.TIMEOUT_SECONDS}s"
            ) from exc
        if res.returncode != 0:
            raise GitCommandError(
                f"git {shlex.join(args)} failed (rc={res.returncode}): "
                f"{res.stderr.strip()}"
            )
        if capture_stderr:
            # `git push` prints progress on stderr — surface it too.
            return (res.stdout or "") + (res.stderr or "")
        return res.stdout

    def _git_run(self, root: Path, args: list[str]) -> None:
        self._git_out(root, args)
