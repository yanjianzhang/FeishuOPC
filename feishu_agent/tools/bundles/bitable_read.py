"""Bundle: ``bitable_read``

Tool surface:
- ``read_bitable_schema`` (effect=read, target=read.bitable)
- ``read_bitable_rows`` (effect=read, target=read.bitable)
- ``resolve_bitable_target`` (effect=read, target=read.bitable)

The handlers use the same role-aware Bitable routing the current
``tool_handlers.resolve_bitable_target`` / ``read_bitable_rows`` /
``read_bitable_schema`` helpers already perform. Migrating the
routing into every bundle would pull the Bitable config loader into
the tools layer, so :class:`BundleContext` supplies the two
callbacks (``load_bitable_tables`` / ``load_role_permissions``) and
an optional ``build_progress_sync_service_for_target`` that scopes
:class:`ProgressSyncService` to the resolved table.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.roles.role_executors.tool_handlers import (
    read_bitable_rows as _legacy_read_rows,
)
from feishu_agent.roles.role_executors.tool_handlers import (
    read_bitable_schema as _legacy_read_schema,
)
from feishu_agent.roles.role_executors.tool_handlers import (
    resolve_bitable_target as _legacy_resolve,
)
from feishu_agent.tools.bundle_context import BundleContext
from feishu_agent.tools.bundle_registry import Handler
from feishu_agent.tools.feishu_agent_tools import (
    ReadBitableRowsArgs,
    ReadBitableSchemaArgs,
    ResolveBitableTargetArgs,
    _tool_spec,
)

_READ_SCHEMA_BASE = _tool_spec(
    "read_bitable_schema",
    "Read the field schema for a Feishu Bitable table your role is permitted to access.",
    ReadBitableSchemaArgs,
)
_READ_ROWS_BASE = _tool_spec(
    "read_bitable_rows",
    "Read existing Feishu Bitable rows from the selected table, optionally filtering by a free-text search string across returned fields.",
    ReadBitableRowsArgs,
)
_RESOLVE_TARGET_BASE = _tool_spec(
    "resolve_bitable_target",
    "Resolve which Feishu Bitable table should be used for the next read or write operation.",
    ResolveBitableTargetArgs,
)


def build_bitable_read_bundle(
    ctx: BundleContext,
) -> list[tuple[AgentToolSpec, Handler]]:
    """Return read-only Bitable tools.

    ``progress_sync_service`` is required as the fallback when no
    per-target builder is supplied (the role-aware routing prefers a
    per-table service when available). If it's missing, we return an
    empty list — the role's frontmatter shouldn't have declared this
    bundle then, but degrading here beats a confusing runtime error.
    """
    progress_sync = ctx.progress_sync_service
    if progress_sync is None:
        return []

    schema_spec = replace(_READ_SCHEMA_BASE, effect="read", target="read.bitable")
    rows_spec = replace(_READ_ROWS_BASE, effect="read", target="read.bitable")
    resolve_spec = replace(
        _RESOLVE_TARGET_BASE, effect="read", target="read.bitable"
    )

    async def _handle_schema(arguments: dict[str, Any]) -> dict[str, Any]:
        parsed = ReadBitableSchemaArgs.model_validate(arguments)
        return await _legacy_read_schema(
            table_name=parsed.table_name,
            role_name=ctx.role_name,
            load_bitable_tables=ctx.load_bitable_tables,
            load_role_permissions=ctx.load_role_permissions,
            sync_service_builder=ctx.build_progress_sync_service_for_target,
            fallback_sync_service=progress_sync,
        )

    async def _handle_rows(arguments: dict[str, Any]) -> dict[str, Any]:
        parsed = ReadBitableRowsArgs.model_validate(arguments)
        return await _legacy_read_rows(
            table_name=parsed.table_name,
            search_text=parsed.search_text,
            field_names=parsed.field_names,
            page_token=parsed.page_token,
            limit=parsed.limit,
            role_name=ctx.role_name,
            load_bitable_tables=ctx.load_bitable_tables,
            load_role_permissions=ctx.load_role_permissions,
            sync_service_builder=ctx.build_progress_sync_service_for_target,
            fallback_sync_service=progress_sync,
        )

    def _handle_resolve(arguments: dict[str, Any]) -> dict[str, Any]:
        parsed = ResolveBitableTargetArgs.model_validate(arguments)
        resolved_name, table = _legacy_resolve(
            table_name=parsed.table_name,
            require_write=parsed.require_write,
            role_name=ctx.role_name,
            load_bitable_tables=ctx.load_bitable_tables,
            load_role_permissions=ctx.load_role_permissions,
        )
        return {
            "table_name": resolved_name,
            "notes": getattr(table, "notes", None),
            "require_write": parsed.require_write,
            "resolved": bool(resolved_name and table),
        }

    return [
        (schema_spec, _handle_schema),
        (rows_spec, _handle_rows),
        (resolve_spec, _handle_resolve),
    ]
