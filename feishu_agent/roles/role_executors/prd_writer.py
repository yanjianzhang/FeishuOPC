from __future__ import annotations

from pathlib import Path
from typing import Any

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.tools.feishu_agent_tools import WriteFileArgs, _tool_spec

PRD_WRITER_TOOL_SPECS = [
    _tool_spec(
        "write_file",
        "Write UTF-8 text content to a file within the allowed specs directory. Creates parent directories as needed.",
        WriteFileArgs,
    ),
]


class PrdWriterExecutor:
    """AgentToolExecutor for the prd_writer role.

    Tools: write_file (scoped to allowed_write_root).
    """

    def __init__(
        self,
        *,
        allowed_write_root: Path,
        **_kwargs: Any,
    ) -> None:
        self._allowed_root = allowed_write_root.resolve()

    def tool_specs(self) -> list[AgentToolSpec]:
        return list(PRD_WRITER_TOOL_SPECS)

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | list[Any] | str:
        if tool_name == "write_file":
            parsed = WriteFileArgs.model_validate(arguments)
            return self._write_file(parsed.path, parsed.content)
        raise RuntimeError(f"Unsupported tool: {tool_name}")

    def _write_file(self, relative_path: str, content: str) -> dict[str, Any]:
        target = (self._allowed_root / relative_path).resolve()
        if not target.is_relative_to(self._allowed_root):
            raise RuntimeError(
                f"Path '{relative_path}' resolves outside the allowed write root. "
                f"All writes must stay within '{self._allowed_root}'."
            )
        parents_existed = target.parent.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {
            "path": str(target.relative_to(self._allowed_root)),
            "bytes_written": len(content.encode("utf-8")),
            "created_parents": not parents_existed,
        }
