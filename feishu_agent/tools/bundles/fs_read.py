"""Bundle: ``fs_read``

Tool surface (when :class:`CodeWriteService` is wired):
- ``describe_code_write_policy`` (effect=read, target=read.fs)
- ``read_project_code`` (effect=read, target=read.fs)
- ``list_project_paths`` (effect=read, target=read.fs)

``grep_project`` is listed in the A-2 spec's initial bundle inventory
but no current backing service exposes a repo-wide grep. Adding it
is tracked as a follow-up; the spec surface is not shipped here
because declaring a tool whose handler does not exist would break
role dispatch rather than degrade gracefully.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.tools.bundle_context import BundleContext
from feishu_agent.tools.bundle_registry import Handler
from feishu_agent.tools.code_write_service import CodeWriteError
from feishu_agent.tools.code_write_tools import (
    CODE_WRITE_TOOL_SPECS,
    ListProjectPathsArgs,
    ReadProjectCodeArgs,
)

_FS_READ_NAMES: frozenset[str] = frozenset(
    {"describe_code_write_policy", "read_project_code", "list_project_paths"}
)


def build_fs_read_bundle(
    ctx: BundleContext,
) -> list[tuple[AgentToolSpec, Handler]]:
    service = ctx.code_write_service
    if service is None:
        return []

    base_specs = {spec.name: spec for spec in CODE_WRITE_TOOL_SPECS}
    items: list[tuple[AgentToolSpec, Handler]] = []

    describe_base = base_specs.get("describe_code_write_policy")
    if describe_base is not None:
        describe_spec = replace(describe_base, effect="read", target="read.fs")

        def _handle_describe(_arguments: dict[str, Any]) -> dict[str, Any]:
            try:
                return service.describe_policy(ctx.project_id)
            except CodeWriteError as exc:
                return {"error": exc.code, "message": exc.message}

        items.append((describe_spec, _handle_describe))

    read_base = base_specs.get("read_project_code")
    if read_base is not None:
        read_spec = replace(read_base, effect="read", target="read.fs")

        def _handle_read(arguments: dict[str, Any]) -> dict[str, Any]:
            parsed = ReadProjectCodeArgs.model_validate(arguments)
            try:
                return service.read_source(
                    project_id=ctx.project_id,
                    relative_path=parsed.relative_path,
                    max_bytes=parsed.max_bytes,
                )
            except CodeWriteError as exc:
                return {"error": exc.code, "message": exc.message}

        items.append((read_spec, _handle_read))

    list_base = base_specs.get("list_project_paths")
    if list_base is not None:
        list_spec = replace(list_base, effect="read", target="read.fs")

        def _handle_list(arguments: dict[str, Any]) -> dict[str, Any]:
            parsed = ListProjectPathsArgs.model_validate(arguments)
            try:
                return service.list_paths(
                    project_id=ctx.project_id,
                    sub_path=parsed.sub_path,
                    max_entries=parsed.max_entries,
                )
            except CodeWriteError as exc:
                return {"error": exc.code, "message": exc.message}

        items.append((list_spec, _handle_list))

    # Defensive: if CODE_WRITE_TOOL_SPECS ever gains new write-adjacent
    # read tools we don't want to silently leak them into fs_read.
    items = [(s, h) for (s, h) in items if s.name in _FS_READ_NAMES]
    return items
