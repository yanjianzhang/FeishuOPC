"""Decorator-registered ``write_file`` — world tool example.

This module demonstrates the M3 pattern for a *world* tool. The tool
is a plain async function that takes its LLM-visible inputs plus a
runtime-injected ``allowed_write_root`` from the per-turn request
context. No mixin, no executor class.

The original :class:`PrdWriterExecutor` (and friends) still define
``write_file`` via the mixin path; this module co-exists with that
code during the migration. Roles that want to migrate simply:

1. Add ``feishu_agent.tools.legacy_tools.file_write`` to the autodiscover
   list (already covered by scanning ``feishu_agent.tools.legacy_tools``).
2. Populate the request context with ``allowed_write_root`` when
   building the executor.
3. Drop the mixin method.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from feishu_agent.tools.tool_registry import tool


class _WriteFileArgs(BaseModel):
    """LLM-visible schema. Mirrors
    :class:`feishu_agent.tools.feishu_agent_tools.WriteFileArgs`
    but lives here so it is regenerated from the Pydantic model
    instead of hand-written JSON schema — easier to diff in PRs."""

    path: str = Field(
        ...,
        description=(
            "Relative file path within the allowed write root "
            "(e.g. 'my-feature/prd.md')"
        ),
        min_length=1,
        max_length=512,
    )
    content: str = Field(..., description="UTF-8 text content to write to the file")


def _schema_of(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    return schema


@tool(
    name="write_file",
    description=(
        "Write UTF-8 text content to a file within the allowed write root. "
        "Creates parent directories as needed."
    ),
    input_schema=_schema_of(_WriteFileArgs),
    effect="world",
    target="world.file.write",
    needs=("allowed_write_root",),
)
async def write_file(
    *,
    path: str,
    content: str,
    allowed_write_root: Path | None,
) -> dict[str, Any]:
    """Write ``content`` to ``path`` under ``allowed_write_root``.

    Safety:
    - ``allowed_write_root`` is injected by the runtime (never by the
      LLM). If unset, the tool refuses — roles that want this tool
      must populate the request context.
    - Paths that resolve outside the root are refused with a
      structured error; we don't raise because the LLM tool loop
      should be able to correct and retry.
    """
    try:
        parsed = _WriteFileArgs.model_validate({"path": path, "content": content})
    except ValidationError as exc:
        return {"ok": False, "error": "INVALID_ARGUMENTS", "detail": exc.errors()}

    if allowed_write_root is None:
        return {
            "ok": False,
            "error": "NO_ALLOWED_WRITE_ROOT",
            "detail": (
                "write_file requires an 'allowed_write_root' context key; "
                "the runtime hasn't provided one for this role."
            ),
        }

    root = Path(allowed_write_root).resolve()
    target = (root / parsed.path).resolve()
    if not target.is_relative_to(root):
        return {
            "ok": False,
            "error": "PATH_ESCAPES_ROOT",
            "detail": (
                f"Path {parsed.path!r} resolves outside the allowed write root "
                f"{str(root)!r}."
            ),
        }

    parents_existed = target.parent.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(parsed.content, encoding="utf-8")
    return {
        "ok": True,
        "path": str(target.relative_to(root)),
        "bytes_written": len(parsed.content.encode("utf-8")),
        "created_parents": not parents_existed,
    }


__all__ = ["write_file"]
