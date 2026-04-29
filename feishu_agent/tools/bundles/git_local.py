"""Bundle: ``git_local``

Tool surface (when :class:`GitOpsService` is wired):
- ``git_commit`` (effect=world, target=world.git.local)

The A-2 spec lists ``git_status`` / ``git_add`` / ``git_log_local``
in the initial inventory, but the MVP repo only exposes ``git_commit``
through :class:`GitOpsService` — staging is handled implicitly by the
commit path (stages every file under ``policy.allowed_write_roots``).
Rather than ship tool specs whose handlers don't exist, we keep the
surface tight and document the gap. Adding those tools is tracked as
a follow-up so role frontmatter and bundle contents stay in sync.

Boundary: git_local operates under ``ctx.working_dir`` (which B-3
will repoint into ``.worktrees/{trace}/``). It does NOT acquire
``repo_filelock`` — that lock is reserved for tools that touch the
single shared remote, i.e. the git_remote bundle.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.tools.bundle_context import BundleContext
from feishu_agent.tools.bundle_registry import Handler
from feishu_agent.tools.code_write_tools import (
    PRE_PUSH_TOOL_SPECS,
    GitCommitArgs,
)
from feishu_agent.tools.git_ops_service import GitOpsError


def build_git_local_bundle(
    ctx: BundleContext,
) -> list[tuple[AgentToolSpec, Handler]]:
    git_ops = ctx.git_ops_service
    if git_ops is None:
        return []

    base_specs = {spec.name: spec for spec in PRE_PUSH_TOOL_SPECS}
    commit_base = base_specs.get("git_commit")
    if commit_base is None:  # pragma: no cover — defensive
        return []

    commit_spec = replace(
        commit_base, effect="world", target="world.git.local"
    )

    def _handle_commit(arguments: dict[str, Any]) -> dict[str, Any]:
        parsed = GitCommitArgs.model_validate(arguments)
        try:
            result = git_ops.commit(
                project_id=ctx.project_id,
                message=parsed.message,
            )
        except GitOpsError as exc:
            return {"error": exc.code, "message": exc.message}
        payload = result.to_dict()
        _emit_commit_line(ctx, payload)
        return payload

    return [(commit_spec, _handle_commit)]


def _emit_commit_line(ctx: BundleContext, result: dict[str, Any]) -> None:
    callback = ctx.thread_update_fn
    if callback is None:
        return
    try:
        branch = result.get("branch", "?")
        sha = (result.get("commit_sha") or "")[:8]
        count = result.get("files_count", "?")
        msg = result.get("message") or ""
        msg_preview = msg.splitlines()[0][:80] if msg else ""
        callback(
            f"📝 git commit {branch}@{sha} ({count} files): {msg_preview}"
        )
    except Exception:  # pragma: no cover — thread push is best-effort
        pass
