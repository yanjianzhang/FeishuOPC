"""Merge a self-state executor and a world executor behind one facade.

Background
----------
The LLM tool loop in :class:`LlmAgentAdapter` talks to a single
:class:`AgentToolExecutor`. Before M3 that executor was whatever the
role provided (e.g. ``DeveloperExecutor``), which in turn collected
every tool via mixin inheritance. Self-state tools (set_mode /
set_plan / todos / note) had nowhere to live in that scheme short
of inheriting a seventh mixin into every role.

The plan's M3 physical split keeps the two layers separate:

- ``TaskStateExecutor`` (``effect="self"``) appends events to the
  per-task log; owns no external IO.
- Role-specific ``WorldExecutor`` (``effect="world"`` / ``"read"``)
  performs filesystem, git, Feishu, LLM, etc. operations.

:class:`CombinedExecutor` is the LLM-facing merge: its ``tool_specs()``
returns the union and ``execute_tool`` dispatches by tool name.
Collisions are resolved "self wins" — if a world executor happens to
expose a tool with the same name as a self-state tool, the self-state
implementation is authoritative. That bias makes self-state a
reserved surface the agent can always rely on.

No allow-list logic here. Role-level allow-lists remain the job of
:class:`AllowListedToolExecutor`, which can wrap a
``CombinedExecutor`` transparently.
"""

from __future__ import annotations

import logging
from typing import Any

from feishu_agent.core.agent_types import AgentToolExecutor, AgentToolSpec

logger = logging.getLogger(__name__)


class CombinedExecutor:
    """LLM-facing merge of a self-state and a world executor.

    Either side may be ``None`` — the combined executor still works,
    which keeps callers free of null-checks during the M3 rollout
    (e.g. a role that hasn't wired self-state yet simply passes
    ``self_executor=None``).
    """

    def __init__(
        self,
        *,
        self_executor: AgentToolExecutor | None,
        world_executor: AgentToolExecutor | None,
    ) -> None:
        self._self = self_executor
        self._world = world_executor
        self._self_names: set[str] = (
            {s.name for s in self_executor.tool_specs()} if self_executor else set()
        )

    # ------------------------------------------------------------------
    # Introspection helpers (useful for tests and diagnostic logging).
    # ------------------------------------------------------------------

    @property
    def self_executor(self) -> AgentToolExecutor | None:
        return self._self

    @property
    def world_executor(self) -> AgentToolExecutor | None:
        return self._world

    # ------------------------------------------------------------------
    # AgentToolExecutor protocol
    # ------------------------------------------------------------------

    def tool_specs(self) -> list[AgentToolSpec]:
        # Self-state specs come first so deterministic iteration
        # reflects the "self wins" collision rule. World specs whose
        # names are shadowed by self are filtered out; this matters
        # because the LLM schema would otherwise show duplicates.
        merged: list[AgentToolSpec] = []
        if self._self is not None:
            merged.extend(self._self.tool_specs())
        if self._world is not None:
            for spec in self._world.tool_specs():
                if spec.name in self._self_names:
                    logger.info(
                        "CombinedExecutor: world tool %r shadowed by self executor",
                        spec.name,
                    )
                    continue
                merged.append(spec)
        return merged

    async def execute_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | list[Any] | str:
        if self._self is not None and tool_name in self._self_names:
            return await self._self.execute_tool(tool_name, arguments)
        if self._world is not None:
            return await self._world.execute_tool(tool_name, arguments)
        return {
            "ok": False,
            "error": f"UNKNOWN_TOOL: {tool_name}",
            "detail": "combined executor has no world executor to forward to",
        }


__all__ = ["CombinedExecutor"]
