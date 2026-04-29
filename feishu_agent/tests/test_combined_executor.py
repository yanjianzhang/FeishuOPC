"""Tests for :class:`CombinedExecutor`."""

from __future__ import annotations

import asyncio
from typing import Any

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.core.combined_executor import CombinedExecutor


class _StubExecutor:
    """Minimal AgentToolExecutor for tests."""

    def __init__(self, specs: list[AgentToolSpec], handlers: dict[str, Any]):
        self._specs = specs
        self._handlers = handlers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def tool_specs(self) -> list[AgentToolSpec]:
        return list(self._specs)

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]):
        self.calls.append((tool_name, arguments))
        return self._handlers[tool_name](arguments)


def _spec(name: str, effect: str = "world") -> AgentToolSpec:
    return AgentToolSpec(
        name=name,
        description=f"stub {name}",
        input_schema={"type": "object"},
        effect=effect,
    )


def test_combined_executor_merges_specs_and_routes_calls():
    self_exec = _StubExecutor(
        specs=[_spec("set_mode", effect="self")],
        handlers={"set_mode": lambda args: {"routed": "self", **args}},
    )
    world_exec = _StubExecutor(
        specs=[_spec("run_tests"), _spec("git_status")],
        handlers={
            "run_tests": lambda args: {"routed": "world.tests"},
            "git_status": lambda args: {"routed": "world.git"},
        },
    )

    combined = CombinedExecutor(self_executor=self_exec, world_executor=world_exec)

    names = [s.name for s in combined.tool_specs()]
    assert names == ["set_mode", "run_tests", "git_status"]

    assert asyncio.run(combined.execute_tool("set_mode", {"mode": "act"})) == {
        "routed": "self",
        "mode": "act",
    }
    assert asyncio.run(combined.execute_tool("run_tests", {})) == {
        "routed": "world.tests"
    }
    assert self_exec.calls == [("set_mode", {"mode": "act"})]
    assert world_exec.calls == [("run_tests", {})]


def test_self_wins_on_name_collision():
    self_exec = _StubExecutor(
        specs=[_spec("note", effect="self")],
        handlers={"note": lambda args: {"from": "self"}},
    )
    world_exec = _StubExecutor(
        specs=[_spec("note"), _spec("ls")],
        handlers={
            "note": lambda args: {"from": "world"},
            "ls": lambda args: {"from": "world.ls"},
        },
    )

    combined = CombinedExecutor(self_executor=self_exec, world_executor=world_exec)

    names = [s.name for s in combined.tool_specs()]
    assert names == ["note", "ls"]  # world.note is shadowed and filtered
    assert asyncio.run(combined.execute_tool("note", {})) == {"from": "self"}
    assert world_exec.calls == []  # world executor never consulted


def test_missing_world_executor_still_routes_self():
    self_exec = _StubExecutor(
        specs=[_spec("set_mode", effect="self")],
        handlers={"set_mode": lambda args: {"ok": True}},
    )
    combined = CombinedExecutor(self_executor=self_exec, world_executor=None)

    assert [s.name for s in combined.tool_specs()] == ["set_mode"]
    assert asyncio.run(combined.execute_tool("set_mode", {})) == {"ok": True}

    # Unknown tool returns structured error rather than raising.
    result = asyncio.run(combined.execute_tool("no_such_tool", {}))
    assert isinstance(result, dict) and result.get("ok") is False
    assert "UNKNOWN_TOOL" in result["error"]


def test_missing_self_executor_forwards_to_world():
    world_exec = _StubExecutor(
        specs=[_spec("run")],
        handlers={"run": lambda args: {"ran": True}},
    )
    combined = CombinedExecutor(self_executor=None, world_executor=world_exec)

    assert [s.name for s in combined.tool_specs()] == ["run"]
    assert asyncio.run(combined.execute_tool("run", {})) == {"ran": True}


def test_no_executors_returns_structured_error():
    combined = CombinedExecutor(self_executor=None, world_executor=None)
    assert combined.tool_specs() == []
    result = asyncio.run(combined.execute_tool("anything", {}))
    assert isinstance(result, dict) and result.get("ok") is False
