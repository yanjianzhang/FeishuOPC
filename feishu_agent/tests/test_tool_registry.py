"""Tests for the decorator-driven tool registry."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest

from feishu_agent.team.task_event_log import TaskKey
from feishu_agent.team.task_service import TaskService
from feishu_agent.tools.tool_registry import (
    ToolRegistry,
    autodiscover,
    role,
    tool,
)

# ---------------------------------------------------------------------------
# Decorator basics
# ---------------------------------------------------------------------------


def test_tool_decorator_registers_with_metadata():
    reg = ToolRegistry()

    @tool(
        name="demo.add",
        description="Add two numbers",
        input_schema={
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
            "required": ["a", "b"],
        },
        effect="self",
        target="self.demo",
        needs=("task_handle",),
        registry=reg,
    )
    async def demo_add(*, a: int, b: int, task_handle: object | None) -> dict:
        return {"ok": True, "sum": a + b, "has_handle": task_handle is not None}

    assert "demo.add" in {e.spec.name for e in reg.list_tools()}
    entry = reg.get_tool("demo.add")
    assert entry is not None
    assert entry.spec.effect == "self"
    assert entry.spec.target == "self.demo"
    assert entry.spec.needs == ("task_handle",)
    assert entry.fn is demo_add
    # attribute reflection for introspection tooling
    assert getattr(demo_add, "__agent_tool_spec__").name == "demo.add"


def test_tool_decorator_rejects_sync_functions():
    reg = ToolRegistry()
    with pytest.raises(TypeError, match="must decorate an async"):

        @tool(
            name="demo.sync",
            description="",
            input_schema={"type": "object"},
            registry=reg,
        )
        def _not_async():  # pragma: no cover — decoration should raise
            return None


def test_role_decorator_records_policy():
    reg = ToolRegistry()

    @role(
        name="tester",
        allow_effects=("self", "read"),
        allow_targets=("self.*", "world.git.*"),
        registry=reg,
    )
    class _Tester:
        pass

    entry = reg.roles["tester"]
    assert entry.cls is _Tester
    assert entry.allow_effects == ("self", "read")
    assert entry.allow_targets == ("self.*", "world.git.*")


# ---------------------------------------------------------------------------
# Executor: needs injection + legacy execution
# ---------------------------------------------------------------------------


def test_executor_injects_needs_and_runs():
    reg = ToolRegistry()

    captured: dict = {}

    @tool(
        name="demo.echo",
        description="Echo with injected context",
        input_schema={
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
        },
        effect="self",
        target="self.demo",
        needs=("task_id", "trace_id"),
        registry=reg,
    )
    async def echo(*, msg: str, task_id: str, trace_id: str) -> dict:
        captured["args"] = (msg, task_id, trace_id)
        return {"ok": True, "msg": msg}

    executor = reg.build_executor(context={"task_id": "T1", "trace_id": "R1"})
    # Only LLM-visible properties are in the spec's schema.
    [spec] = [s for s in executor.tool_specs() if s.name == "demo.echo"]
    assert set(spec.input_schema["properties"]) == {"msg"}
    result = asyncio.run(executor.execute_tool("demo.echo", {"msg": "hi"}))
    assert result == {"ok": True, "msg": "hi"}
    assert captured["args"] == ("hi", "T1", "R1")


def test_executor_missing_context_passes_none():
    reg = ToolRegistry()

    @tool(
        name="demo.needs",
        description="",
        input_schema={"type": "object"},
        needs=("task_id",),
        registry=reg,
    )
    async def _needs(*, task_id) -> dict:  # noqa: ANN001
        return {"ok": True, "task_id": task_id}

    executor = reg.build_executor(context={})  # empty: task_id missing
    result = asyncio.run(executor.execute_tool("demo.needs", {}))
    assert result == {"ok": True, "task_id": None}


def test_executor_unknown_tool_returns_structured_error():
    reg = ToolRegistry()
    executor = reg.build_executor()
    result = asyncio.run(executor.execute_tool("does.not.exist", {}))
    assert isinstance(result, dict) and result.get("ok") is False
    assert "UNKNOWN_TOOL" in result["error"]


def test_executor_drops_llm_supplied_needs_key():
    """The LLM must not be able to spoof a ``needs``-injected context key.

    If an LLM tool call includes an argument whose name matches a
    ``needs`` entry (e.g. forging ``task_handle`` to poke the registry),
    the runtime-injected value must still win and the spoofed value
    must be silently dropped — not produce a ``TypeError`` collision.
    """
    reg = ToolRegistry()
    captured: dict = {}

    @tool(
        name="demo.inject",
        description="",
        input_schema={
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
        },
        needs=("task_handle",),
        registry=reg,
    )
    async def _inject(*, msg: str, task_handle) -> dict:  # noqa: ANN001
        captured["task_handle"] = task_handle
        captured["msg"] = msg
        return {"ok": True}

    real_handle = object()
    executor = reg.build_executor(context={"task_handle": real_handle})
    # The LLM tries to supply its own ``task_handle`` value.
    result = asyncio.run(
        executor.execute_tool(
            "demo.inject",
            {"msg": "hello", "task_handle": "SPOOFED"},
        )
    )
    assert result == {"ok": True}
    # Runtime-injected value wins; the LLM-supplied value is discarded.
    assert captured["task_handle"] is real_handle
    assert captured["msg"] == "hello"


def test_executor_type_error_surfaces_as_invalid_arguments():
    reg = ToolRegistry()

    @tool(
        name="demo.strict",
        description="",
        input_schema={
            "type": "object",
            "properties": {"x": {"type": "integer"}},
            "required": ["x"],
        },
        registry=reg,
    )
    async def _strict(*, x: int) -> dict:
        return {"ok": True, "x": x}

    executor = reg.build_executor()
    result = asyncio.run(
        executor.execute_tool("demo.strict", {"x": 1, "unexpected_kwarg": 2})
    )
    assert isinstance(result, dict)
    assert result.get("ok") is False
    assert result["error"] == "INVALID_ARGUMENTS"


# ---------------------------------------------------------------------------
# Autodiscover
# ---------------------------------------------------------------------------


def test_autodiscover_imports_self_state_module():
    # The real module may already be imported by a previous test — that's
    # expected. autodiscover should still return the module name and the
    # registry should hold the canonical tools.
    from feishu_agent.tools import tool_registry as tr

    imported = autodiscover(["feishu_agent.tools.legacy_tools"])
    assert "feishu_agent.tools.legacy_tools.self_state" in imported

    # The global registry picks up the canonical self-state tools once
    # their module is imported.
    names = {e.spec.name for e in tr.GLOBAL_TOOL_REGISTRY.list_tools()}
    assert {"set_mode", "set_plan", "add_todo", "mark_todo_done", "note"} <= names


# ---------------------------------------------------------------------------
# End-to-end: self-state tool flows through the registry executor
# ---------------------------------------------------------------------------


def test_decorator_self_state_tool_writes_to_task_log(tmp_path: Path):
    # Ensure the canonical self-state module is loaded.
    importlib.import_module("feishu_agent.tools.legacy_tools.self_state")
    from feishu_agent.tools import tool_registry as tr

    svc = TaskService(tasks_root=tmp_path)
    key = TaskKey(bot_name="bot", chat_id="c1", root_id="r1")
    handle = svc.open_or_resume(key, role_name="tester")

    executor = tr.GLOBAL_TOOL_REGISTRY.build_executor(
        tool_names=["set_mode"],
        context={"task_handle": handle},
    )
    result = asyncio.run(
        executor.execute_tool("set_mode", {"mode": "plan", "reason": "thinking"})
    )
    assert result["ok"] is True

    kinds = [e.kind for e in handle.log.read_events()]
    assert "state.mode_set" in kinds
