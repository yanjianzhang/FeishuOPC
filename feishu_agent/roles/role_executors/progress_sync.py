from __future__ import annotations

from typing import Any, Callable

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.roles.role_executors.tool_handlers import (
    build_sync_service_for_target,
    resolve_bitable_target,
)
from feishu_agent.schemas.progress_sync import ProgressSyncRequest
from feishu_agent.team.sprint_state_service import SprintStateService
from feishu_agent.tools.feishu_agent_tools import (
    ResolveBitableTargetArgs,
    SyncProgressArgs,
    _tool_spec,
)
from feishu_agent.tools.progress_sync_service import ProgressSyncService

PROGRESS_SYNC_TOOL_SPECS = [
    _tool_spec(
        "preview_progress_sync",
        "Preview the project progress rows that would be synced to Feishu Bitable without writing them.",
        SyncProgressArgs,
    ),
    _tool_spec(
        "write_progress_sync",
        "Write the current project progress rows into Feishu Bitable.",
        SyncProgressArgs,
    ),
    _tool_spec(
        "resolve_bitable_target",
        "Resolve which Feishu Bitable table should be used for the next read or write operation.",
        ResolveBitableTargetArgs,
    ),
]


class ProgressSyncExecutor:
    """AgentToolExecutor for the progress_sync role.

    Tools: preview_progress_sync, write_progress_sync, resolve_bitable_target.
    """

    def __init__(
        self,
        *,
        sprint_state_service: SprintStateService,
        progress_sync_service: ProgressSyncService,
        project_id: str = "",
        command_text: str = "",
        role_name: str = "progress_sync",
        trace_id: str = "",
        chat_id: str | None = None,
        load_bitable_tables: Callable[[], dict[str, Any]] = lambda: {},
        load_role_permissions: Callable[[str], list[Any]] = lambda _: [],
        build_progress_sync_service_for_target: Callable[[str | None, Any | None], ProgressSyncService] | None = None,
    ) -> None:
        self._sprint_state = sprint_state_service
        self._progress_sync = progress_sync_service
        self.project_id = project_id
        self.command_text = command_text
        self.role_name = role_name
        self.trace_id = trace_id
        self.chat_id = chat_id
        self.load_bitable_tables = load_bitable_tables
        self.load_role_permissions = load_role_permissions
        self._build_progress_sync_service_for_target = build_progress_sync_service_for_target
        self.last_table_name: str | None = None

    def tool_specs(self) -> list[AgentToolSpec]:
        return list(PROGRESS_SYNC_TOOL_SPECS)

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | list[Any] | str:
        if tool_name == "resolve_bitable_target":
            parsed = ResolveBitableTargetArgs.model_validate(arguments)
            return self._resolve_bitable_target_payload(parsed.table_name, parsed.require_write)
        if tool_name == "preview_progress_sync":
            parsed = SyncProgressArgs.model_validate(arguments)
            return await self._run_progress_sync(mode="preview", module=parsed.module, table_name=parsed.table_name)
        if tool_name == "write_progress_sync":
            parsed = SyncProgressArgs.model_validate(arguments)
            return await self._run_progress_sync(mode="write", module=parsed.module, table_name=parsed.table_name)
        raise RuntimeError(f"Unsupported tool: {tool_name}")

    def _resolve_bitable_target_payload(self, table_name: str | None, require_write: bool) -> dict[str, Any]:
        resolved_name, table = resolve_bitable_target(
            table_name=table_name,
            require_write=require_write,
            role_name=self.role_name,
            load_bitable_tables=self.load_bitable_tables,
            load_role_permissions=self.load_role_permissions,
        )
        self.last_table_name = resolved_name
        return {
            "table_name": resolved_name,
            "notes": getattr(table, "notes", None),
            "require_write": require_write,
            "resolved": bool(resolved_name and table),
        }

    async def _run_progress_sync(self, *, mode: str, module: str | None, table_name: str | None) -> dict[str, Any]:
        resolved_table_name, bitable_target = resolve_bitable_target(
            table_name=table_name,
            require_write=mode == "write",
            role_name=self.role_name,
            load_bitable_tables=self.load_bitable_tables,
            load_role_permissions=self.load_role_permissions,
        )
        service = build_sync_service_for_target(
            resolved_table_name, bitable_target,
            builder=self._build_progress_sync_service_for_target,
            fallback=self._progress_sync,
        )
        command_text = f"同步 {module}" if module else "同步当前进度"
        result = await service.execute(
            ProgressSyncRequest(
                project_id=self.project_id,
                command_text=command_text,
                mode=mode,
                trace_id=self.trace_id,
                chat_id=self.chat_id,
            )
        )
        return {
            "ok": result.ok,
            "mode": result.mode,
            "message": result.message,
            "target_table_name": resolved_table_name,
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
