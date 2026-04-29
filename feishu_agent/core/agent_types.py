from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

# Canonical effect categories for two-layer tool governance.
# - ``"self"`` mutates the agent's task state only (no external IO).
# - ``"world"`` hits the filesystem, git, Feishu, or any external system.
# - ``"read"`` is a read-only world touch (grep, read_repo_file, etc.) — the
#   allow-policy usually treats "read" more leniently than "world" writes.
# Fielded as a plain string so decorators can extend later without breaking
# backwards compatibility (unknown values default to ``"world"`` treatment).
Effect = str


@dataclass(frozen=True)
class AgentToolSpec:
    """Declarative description of a tool exposed to the LLM.

    The first three fields (``name`` / ``description`` / ``input_schema``)
    are what the LLM sees. The ``effect`` / ``target`` / ``needs`` fields
    are runtime metadata: they drive role-level allow-lists and the auto-
    injection of per-call context parameters (task_id / chat_id / …)
    that must NEVER be visible in the LLM's schema.

    All three extra fields are optional with backwards-compatible
    defaults, so existing ``AgentToolSpec(name=…, description=…,
    input_schema=…)`` callers continue to work.
    """

    name: str
    description: str
    input_schema: dict[str, Any]

    # --- M3 metadata (all optional, defaults = "legacy world tool") ---

    # "self" | "world" | "read". See :data:`Effect` above. Default is
    # ``"world"`` so any tool created via the legacy 3-arg constructor
    # behaves exactly like before (policy treats it as world-effecting).
    effect: Effect = "world"

    # Glob-style scope string the allow-policy matches against. Examples:
    # ``"world.git.*"`` / ``"self.plan"`` / ``"*"`` (anything). Defaults
    # to ``"*"`` so an unlabeled legacy tool is matchable by
    # role-level ``allow_targets=["*"]``.
    target: str = "*"

    # Named runtime context the tool needs injected at call time. These
    # parameters are supplied by the adapter when invoking the tool; they
    # never appear in ``input_schema`` (the LLM can't forge them). Typical
    # values: ``"task_id"``, ``"chat_id"``, ``"project_id"``,
    # ``"trace_id"``, ``"thread_update_fn"``, ``"task_handle"``.
    needs: tuple[str, ...] = ()

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.input_schema,
            "strict": True,
        }

    def to_openai_chat_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    def to_anthropic_tool(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass(frozen=True)
class AgentToolCall:
    call_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class AgentToolResult:
    call_id: str
    tool_name: str
    output: dict[str, Any] | list[Any] | str
    is_error: bool = False

    def to_jsonable_output(self) -> Any:
        return self.output


class AgentToolExecutor(Protocol):
    def tool_specs(self) -> list[AgentToolSpec]:
        ...

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | list[Any] | str:
        ...


class AllowListedToolExecutor:
    """Decorator that restricts an underlying executor to a fixed allow-list.

    - `tool_specs()` returns only specs whose name is in `allow`.
    - `execute_tool()` refuses tools outside `allow` with a structured error
      instead of raising, so the LLM tool loop can recover.
    - `allow=None` means "no restriction" — pass through.
    """

    def __init__(self, inner: AgentToolExecutor, allow: list[str] | None) -> None:
        self._inner = inner
        self._allow: set[str] | None = set(allow) if allow else None

    def tool_specs(self) -> list[AgentToolSpec]:
        specs = list(self._inner.tool_specs())
        if self._allow is None:
            return specs
        return [s for s in specs if s.name in self._allow]

    async def execute_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | list[Any] | str:
        if self._allow is not None and tool_name not in self._allow:
            return {
                "error": f"TOOL_NOT_ALLOWED: {tool_name}",
                "allowed": sorted(self._allow),
            }
        return await self._inner.execute_tool(tool_name, arguments)
