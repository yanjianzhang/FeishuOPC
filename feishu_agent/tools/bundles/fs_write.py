"""Bundle: ``fs_write``

Tool surface:
- ``write_project_code`` (effect=world, target=world.fs.code)
- ``write_project_code_batch`` (effect=world, target=world.fs.code)
- ``write_role_artifact`` (effect=self, target=self.artifact)
  — the agent's own output channel, scoped to a per-role subdir via
  ``RoleArtifactWriter``. Classified as ``self`` (not ``world``)
  because the blast radius is strictly the agent's own directory:
  UTF-8 text only, path-contained, size-capped, not committable
  without an explicit git tool. This aligns with ``advance_sprint_state``
  (also ``self``) and keeps the 7 migrating roles — whose frontmatter
  is ``allow_effects=[read, self]`` / ``allow_targets=["read.*",
  "self.*"]`` — functional after Wave 3.

The three tools live together because they all produce durable
filesystem artifacts under the repo. Role frontmatter uses the
``allow_effects=[...]`` / ``allow_targets=[...]`` filters to decide
which subset to expose.

``write_file`` (prd_writer's legacy tool) is intentionally NOT part
of this bundle: its allowed root is the PRD ``specs/`` tree, a
different path than ``RoleArtifactWriter._allowed_root``
(``reviews/<role>/``). Folding the two into one bundle would make
prd_writer silently write to the wrong directory after Wave 3.
Wave 3 will decide whether prd_writer migrates to
``write_role_artifact`` semantics or keeps a dedicated executor.
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
    WriteProjectCodeArgs,
    WriteProjectCodeBatchArgs,
)


def build_fs_write_bundle(
    ctx: BundleContext,
) -> list[tuple[AgentToolSpec, Handler]]:
    items: list[tuple[AgentToolSpec, Handler]] = []

    # --- write_project_code / write_project_code_batch ---
    service = ctx.code_write_service
    if service is not None:
        base_specs = {spec.name: spec for spec in CODE_WRITE_TOOL_SPECS}
        write_base = base_specs.get("write_project_code")
        if write_base is not None:
            write_spec = replace(
                write_base, effect="world", target="world.fs.code"
            )

            def _handle_write(arguments: dict[str, Any]) -> dict[str, Any]:
                parsed = WriteProjectCodeArgs.model_validate(arguments)
                try:
                    res = service.write_source(
                        project_id=ctx.project_id,
                        relative_path=parsed.relative_path,
                        content=parsed.content,
                        reason=parsed.reason,
                        confirmed=parsed.confirmed,
                    )
                except CodeWriteError as exc:
                    return {"error": exc.code, "message": exc.message}
                _emit_write_line(ctx, res, multi=False)
                return res

            items.append((write_spec, _handle_write))

        batch_base = base_specs.get("write_project_code_batch")
        if batch_base is not None:
            batch_spec = replace(
                batch_base, effect="world", target="world.fs.code"
            )

            def _handle_batch(arguments: dict[str, Any]) -> dict[str, Any]:
                parsed = WriteProjectCodeBatchArgs.model_validate(arguments)
                try:
                    res = service.write_sources_batch(
                        project_id=ctx.project_id,
                        files=[
                            {
                                "relative_path": f.relative_path,
                                "content": f.content,
                                "reason": f.reason or parsed.reason,
                            }
                            for f in parsed.files
                        ],
                        reason=parsed.reason,
                        confirmed=parsed.confirmed,
                    )
                except CodeWriteError as exc:
                    return {"error": exc.code, "message": exc.message}
                _emit_write_line(ctx, res, multi=True)
                return res

            items.append((batch_spec, _handle_batch))

    # --- write_role_artifact (via RoleArtifactWriter) ---
    #
    # Classified as ``effect="self", target="self.artifact"`` so that
    # roles with ``allow_effects=[read, self]`` / ``allow_targets=
    # ["read.*", "self.*"]`` retain this tool through BundleRegistry
    # filtering. See module docstring for the rationale.
    artifact_writer = ctx.role_artifact_writer
    if artifact_writer is not None:
        for base_spec in artifact_writer.tool_specs():
            tagged = replace(base_spec, effect="self", target="self.artifact")

            def _handle_artifact(
                arguments: dict[str, Any], _name: str = base_spec.name
            ) -> dict[str, Any]:
                handled = artifact_writer.try_handle(_name, arguments)
                if handled is None:
                    return {
                        "error": "ROLE_ARTIFACT_TOOL_NOT_DISPATCHED",
                        "message": (
                            f"RoleArtifactWriter returned None for "
                            f"{_name!r}. This indicates a wiring bug; "
                            "the writer should recognise every spec it "
                            "advertises via tool_specs()."
                        ),
                    }
                return handled

            items.append((tagged, _handle_artifact))

    return items


def _emit_write_line(
    ctx: BundleContext, result: dict[str, Any], *, multi: bool
) -> None:
    """Best-effort thread-update line mirroring CodeWriteToolsMixin UX.

    Mirrors ``_push_write_line`` in ``code_write_tools.py`` so users
    see the same "✏️ 代码写入 …" update whether the tool runs through
    TL or a bundled role.
    """
    callback = ctx.thread_update_fn
    if callback is None:
        return
    try:
        if multi:
            files = result.get("files") or []
            total_bytes = sum(int(f.get("bytes_written") or 0) for f in files)
            paths = ", ".join(f.get("path", "?") for f in files[:3])
            suffix = "" if len(files) <= 3 else f" +{len(files) - 3} more"
            callback(
                f"✏️ 代码批量写入 {len(files)} 文件 / {total_bytes}B: {paths}{suffix}"
            )
        else:
            path = result.get("path", "?")
            bw = int(result.get("bytes_written") or 0)
            icon = "➕" if result.get("is_new_file") else "✏️"
            delta = int(result.get("bytes_delta") or 0)
            delta_str = (
                f" (Δ{delta:+d}B)" if not result.get("is_new_file") else ""
            )
            callback(f"{icon} 代码写入 {path} / {bw}B{delta_str}")
    except Exception:  # pragma: no cover — thread push is best-effort
        pass
