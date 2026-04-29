"""Bundle: ``bitable_write``

Tool surface:
- ``preview_progress_sync`` (effect=read, target=read.bitable)
  — dry-run preview is pure read. Same name kept so the LLM's mental
  model ("preview then confirm then write") stays intact.
- ``write_progress_sync`` (effect=world, target=world.bitable)
  — actually writes rows to Feishu Bitable.

The existing ``progress_sync`` role executor stays a custom executor
in A-2 (it needs :class:`PendingActionService` wiring the bundle
interface cannot express yet), so this bundle is prepared for future
migrations — e.g. a light-weight reviewer variant that only previews.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.roles.role_executors.tool_handlers import (
    build_sync_service_for_target,
)
from feishu_agent.roles.role_executors.tool_handlers import (
    resolve_bitable_target as _resolve,
)
from feishu_agent.schemas.progress_sync import ProgressSyncRequest
from feishu_agent.tools.bundle_context import BundleContext
from feishu_agent.tools.bundle_registry import Handler
from feishu_agent.tools.feishu_agent_tools import (
    SyncProgressArgs,
    _tool_spec,
)

_PREVIEW_BASE = _tool_spec(
    "preview_progress_sync",
    "Preview the project progress rows that would be synced to Feishu Bitable without writing them.",
    SyncProgressArgs,
)
_WRITE_BASE = _tool_spec(
    "write_progress_sync",
    "Write the current project progress rows into Feishu Bitable.",
    SyncProgressArgs,
)


def build_bitable_write_bundle(
    ctx: BundleContext,
) -> list[tuple[AgentToolSpec, Handler]]:
    progress_sync = ctx.progress_sync_service
    if progress_sync is None:
        return []

    preview_spec = replace(_PREVIEW_BASE, effect="read", target="read.bitable")
    write_spec = replace(_WRITE_BASE, effect="world", target="world.bitable")

    async def _run(
        *, mode: str, module: str | None, table_name: str | None
    ) -> dict[str, Any]:
        resolved_name, table = _resolve(
            table_name=table_name,
            require_write=(mode == "write"),
            role_name=ctx.role_name,
            load_bitable_tables=ctx.load_bitable_tables,
            load_role_permissions=ctx.load_role_permissions,
        )
        service = build_sync_service_for_target(
            resolved_name,
            table,
            builder=ctx.build_progress_sync_service_for_target,
            fallback=progress_sync,
        )
        command_text = f"同步 {module}" if module else "同步当前进度"
        result = await service.execute(
            ProgressSyncRequest(
                project_id=ctx.project_id,
                command_text=command_text,
                mode=mode,
                trace_id=ctx.trace_id,
                chat_id=ctx.chat_id,
            )
        )
        return {
            "ok": result.ok,
            "mode": result.mode,
            "message": result.message,
            "target_table_name": resolved_name,
            "summary": result.summary.model_dump(),
            "warnings": result.warnings,
            "errors": result.errors,
            "write_result": None
            if result.write_result is None
            else {
                "created": result.write_result.created,
                "updated": result.write_result.updated,
                "skipped": result.write_result.skipped,
                "failed": result.write_result.failed,
            },
        }

    async def _handle_preview(arguments: dict[str, Any]) -> dict[str, Any]:
        parsed = SyncProgressArgs.model_validate(arguments)
        return await _run(
            mode="preview", module=parsed.module, table_name=parsed.table_name
        )

    async def _handle_write(arguments: dict[str, Any]) -> dict[str, Any]:
        parsed = SyncProgressArgs.model_validate(arguments)
        return await _run(
            mode="write", module=parsed.module, table_name=parsed.table_name
        )

    return [
        (preview_spec, _handle_preview),
        (write_spec, _handle_write),
    ]
