"""Shared tool handler helpers used by multiple role executors.

Extract-once pattern: if 3+ executors need the same handler body,
it belongs here rather than being duplicated in each executor.
"""

from __future__ import annotations

from typing import Any, Callable

from feishu_agent.team.sprint_state_service import SprintStateService
from feishu_agent.tools.progress_sync_service import ProgressSyncService


def read_sprint_status(sprint_state: SprintStateService, sprint: str | None) -> dict[str, Any]:
    _ = sprint
    data = sprint_state.load_status_data()
    current_sprint = data.get("current_sprint") or {}
    return {
        "sprint_name": data.get("sprint_name"),
        "goal": current_sprint.get("goal"),
        "current_sprint": current_sprint,
    }


def resolve_bitable_target(
    *,
    table_name: str | None,
    require_write: bool,
    role_name: str,
    load_bitable_tables: Callable[[], dict[str, Any]],
    load_role_permissions: Callable[[str], list[Any]],
) -> tuple[str | None, Any | None]:
    """Resolve which Bitable table to use for a role, honouring permissions."""
    tables = load_bitable_tables()
    permissions = load_role_permissions(role_name)
    candidates: list[tuple[Any, Any]] = []
    for permission in permissions:
        table = tables.get(getattr(permission, "table_name", ""))
        if not table:
            continue
        if require_write and not getattr(permission, "can_write", False):
            continue
        if not require_write and not (getattr(permission, "can_read", False) or getattr(permission, "can_write", False)):
            continue
        candidates.append((permission, table))

    if not candidates:
        return None, None

    if table_name:
        for permission, table in candidates:
            if getattr(permission, "table_name", None) == table_name:
                return table_name, table
        raise RuntimeError(f"Table '{table_name}' is not available for role '{role_name}'.")

    default_candidate = next(
        (item for item in candidates if getattr(item[0], "is_default", False)),
        candidates[0],
    )
    return getattr(default_candidate[0], "table_name", None), default_candidate[1]


def build_sync_service_for_target(
    table_name: str | None,
    bitable_target: Any | None,
    *,
    builder: Callable[[str | None, Any | None], ProgressSyncService] | None,
    fallback: ProgressSyncService,
) -> ProgressSyncService:
    """Build or return a ProgressSyncService scoped to a resolved Bitable target."""
    if builder:
        return builder(table_name, bitable_target)
    return fallback


async def read_bitable_rows(
    *,
    table_name: str | None,
    search_text: str | None,
    field_names: list[str] | None,
    page_token: str | None,
    limit: int,
    role_name: str,
    load_bitable_tables: Callable[[], dict[str, Any]],
    load_role_permissions: Callable[[str], list[Any]],
    sync_service_builder: Callable[[str | None, Any | None], ProgressSyncService] | None,
    fallback_sync_service: ProgressSyncService,
) -> dict[str, Any]:
    """Shared read_bitable_rows handler used by multiple executors."""
    resolved_table_name, bitable_target = resolve_bitable_target(
        table_name=table_name,
        require_write=False,
        role_name=role_name,
        load_bitable_tables=load_bitable_tables,
        load_role_permissions=load_role_permissions,
    )
    if not bitable_target:
        raise RuntimeError(f"No readable Feishu Bitable target is available for role '{role_name}'.")
    service = build_sync_service_for_target(
        resolved_table_name, bitable_target,
        builder=sync_service_builder,
        fallback=fallback_sync_service,
    )
    rows_payload = await service.read_bitable_rows(
        table_name=resolved_table_name,
        search_text=search_text,
        field_names=field_names,
        page_token=page_token,
        limit=limit,
        auth_mode="tenant",
    )
    rows_payload["target_table_name"] = resolved_table_name
    return rows_payload


async def read_bitable_schema(
    *,
    table_name: str | None,
    role_name: str,
    load_bitable_tables: Callable[[], dict[str, Any]],
    load_role_permissions: Callable[[str], list[Any]],
    sync_service_builder: Callable[[str | None, Any | None], ProgressSyncService] | None,
    fallback_sync_service: ProgressSyncService,
) -> dict[str, Any]:
    """Shared read_bitable_schema handler used by reviewer and other read roles."""
    resolved_table_name, bitable_target = resolve_bitable_target(
        table_name=table_name,
        require_write=False,
        role_name=role_name,
        load_bitable_tables=load_bitable_tables,
        load_role_permissions=load_role_permissions,
    )
    if not bitable_target:
        raise RuntimeError(f"No readable Feishu Bitable target is available for role '{role_name}'.")
    service = build_sync_service_for_target(
        resolved_table_name, bitable_target,
        builder=sync_service_builder,
        fallback=fallback_sync_service,
    )
    schema = await service.read_bitable_schema(table_name=resolved_table_name, auth_mode="tenant")
    schema["target_table_name"] = resolved_table_name
    return schema
