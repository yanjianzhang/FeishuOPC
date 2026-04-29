"""Bundle: ``git_remote``

Tool surface:
- ``git_push`` (effect=world, target=world.git.remote)
- ``git_sync_remote`` (effect=world, target=world.git.remote)
- ``create_pull_request`` (effect=world, target=world.git.remote)

Every tool here is a world-touching remote operation. Per A-2 / B-3,
they MUST run against ``ctx.repo_root`` (the single push-safe working
tree) even when the role otherwise operates inside a worktree.
B-2 will introduce the concurrency group ``world.git.remote`` so two
roles cannot interleave a push / PR creation; the ``effect``/``target``
labels here are how the fan-out scheduler will recognize them.

``start_work_branch`` is intentionally NOT in this bundle even though
it performs a fetch: it is tech_lead-only in the MVP (branch
lifecycle belongs to the gatekeeper), and TL remains a custom
executor. Adding it to git_remote would widen the surface the 7
migrated roles could request via frontmatter.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.tools.bundle_context import BundleContext
from feishu_agent.tools.bundle_registry import Handler
from feishu_agent.tools.code_write_tools import (
    PRE_PUSH_TOOL_SPECS,
    CreatePullRequestArgs,
    GitPushArgs,
    GitSyncRemoteArgs,
)
from feishu_agent.tools.git_ops_service import GitOpsError
from feishu_agent.tools.pull_request_service import PullRequestError


def build_git_remote_bundle(
    ctx: BundleContext,
) -> list[tuple[AgentToolSpec, Handler]]:
    items: list[tuple[AgentToolSpec, Handler]] = []
    base_specs = {spec.name: spec for spec in PRE_PUSH_TOOL_SPECS}

    git_ops = ctx.git_ops_service
    if git_ops is not None:
        push_base = base_specs.get("git_push")
        if push_base is not None:
            push_spec = replace(
                push_base, effect="world", target="world.git.remote"
            )

            def _handle_push(arguments: dict[str, Any]) -> dict[str, Any]:
                parsed = GitPushArgs.model_validate(arguments)
                try:
                    result = git_ops.push_current_branch(
                        project_id=ctx.project_id,
                        inspection_token=parsed.inspection_token,
                        remote=parsed.remote,
                    )
                except GitOpsError as exc:
                    return {"error": exc.code, "message": exc.message}
                payload = result.to_dict()
                _emit(ctx, f"🚀 git push {payload.get('branch', '?')}")
                return payload

            items.append((push_spec, _handle_push))

        sync_base = base_specs.get("git_sync_remote")
        if sync_base is not None:
            sync_spec = replace(
                sync_base, effect="world", target="world.git.remote"
            )

            def _handle_sync(arguments: dict[str, Any]) -> dict[str, Any]:
                parsed = GitSyncRemoteArgs.model_validate(arguments)
                try:
                    result = git_ops.sync_with_remote(
                        project_id=ctx.project_id,
                        remote=parsed.remote,
                    )
                except GitOpsError as exc:
                    return {"error": exc.code, "message": exc.message}
                payload = result.to_dict()
                _emit(ctx, f"🔄 git sync {payload.get('branch', '?')}")
                return payload

            items.append((sync_spec, _handle_sync))

    pr_service = ctx.pull_request_service
    if pr_service is not None:
        pr_base = base_specs.get("create_pull_request")
        if pr_base is not None:
            pr_spec = replace(
                pr_base, effect="world", target="world.git.remote"
            )

            def _handle_pr(arguments: dict[str, Any]) -> dict[str, Any]:
                parsed = CreatePullRequestArgs.model_validate(arguments)
                try:
                    result = pr_service.create_pull_request(
                        project_id=ctx.project_id,
                        title=parsed.title,
                        body=parsed.body,
                        base=parsed.base,
                    )
                except PullRequestError as exc:
                    return {"error": exc.code, "message": exc.message}
                payload = result.to_dict()
                _emit(
                    ctx,
                    f"📬 PR #{payload.get('number', '?')}: {payload.get('url', '')}",
                )
                return payload

            items.append((pr_spec, _handle_pr))

    return items


def _emit(ctx: BundleContext, line: str) -> None:
    callback = ctx.thread_update_fn
    if callback is None:
        return
    try:
        callback(line)
    except Exception:  # pragma: no cover
        pass
