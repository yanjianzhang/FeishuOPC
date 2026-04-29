"""Unit tests for :class:`BundleRegistry` and :class:`_CompositeExecutor`.

Covers the contract required by 004 A-2:
- ``register`` refuses duplicate names.
- ``build`` composes multiple bundles into one executor.
- ``BundleNotFoundError`` on unknown name, with the known bundle list.
- ``ToolNameCollisionError`` when two bundles export the same tool.
- ``allow_effects`` filters by effect (set membership).
- ``allow_targets`` filters by fnmatch glob.
- Executor dispatches to the right handler and preserves spec order.
- Awaitable handlers and sync handlers both work.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.tools.bundle_context import BundleContext
from feishu_agent.tools.bundle_registry import (
    BundleNotFoundError,
    BundleRegistry,
    ToolNameCollisionError,
)


def _make_ctx(tmp_path: Path) -> BundleContext:
    return BundleContext(
        working_dir=tmp_path,
        repo_root=tmp_path,
        chat_id="chat-1",
        trace_id="trace-1",
        role_name="test_role",
    )


def _spec(name: str, effect: str = "read", target: str = "*") -> AgentToolSpec:
    return AgentToolSpec(
        name=name,
        description=f"Test tool {name}",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effect=effect,
        target=target,
    )


def test_register_and_build_single_bundle(tmp_path: Path) -> None:
    reg = BundleRegistry()
    reg.register(
        "alpha",
        lambda ctx: [
            (_spec("tool_a"), lambda args: {"ok": True, "tool": "a"}),
            (_spec("tool_b"), lambda args: {"ok": True, "tool": "b"}),
        ],
    )
    executor = reg.build(["alpha"], _make_ctx(tmp_path))
    names = [s.name for s in executor.tool_specs()]
    assert names == ["tool_a", "tool_b"]


def test_register_refuses_duplicates() -> None:
    reg = BundleRegistry()
    reg.register("alpha", lambda ctx: [])
    with pytest.raises(ValueError, match="already registered"):
        reg.register("alpha", lambda ctx: [])


def test_build_unknown_bundle_raises_with_known_list(tmp_path: Path) -> None:
    reg = BundleRegistry()
    reg.register("alpha", lambda ctx: [])
    reg.register("beta", lambda ctx: [])
    with pytest.raises(BundleNotFoundError) as excinfo:
        reg.build(["gamma"], _make_ctx(tmp_path))
    assert excinfo.value.name == "gamma"
    assert excinfo.value.known == ["alpha", "beta"]


def test_build_collision_across_bundles_raises(tmp_path: Path) -> None:
    reg = BundleRegistry()
    reg.register(
        "alpha",
        lambda ctx: [(_spec("shared"), lambda a: "alpha")],
    )
    reg.register(
        "beta",
        lambda ctx: [(_spec("shared"), lambda a: "beta")],
    )
    with pytest.raises(ToolNameCollisionError) as excinfo:
        reg.build(["alpha", "beta"], _make_ctx(tmp_path))
    assert excinfo.value.tool_name == "shared"


def test_allow_effects_filter_excludes_world(tmp_path: Path) -> None:
    reg = BundleRegistry()
    reg.register(
        "mixed",
        lambda ctx: [
            (_spec("r1", effect="read"), lambda a: "r1"),
            (_spec("s1", effect="self"), lambda a: "s1"),
            (_spec("w1", effect="world"), lambda a: "w1"),
        ],
    )
    executor = reg.build(
        ["mixed"], _make_ctx(tmp_path), allow_effects=["read", "self"]
    )
    names = [s.name for s in executor.tool_specs()]
    assert names == ["r1", "s1"]


def test_allow_targets_fnmatch_glob(tmp_path: Path) -> None:
    reg = BundleRegistry()
    reg.register(
        "targets",
        lambda ctx: [
            (_spec("read_code", effect="read", target="read.fs"), lambda a: 1),
            (_spec("read_git", effect="read", target="read.git"), lambda a: 2),
            (_spec("write_fs", effect="world", target="world.fs"), lambda a: 3),
        ],
    )
    executor = reg.build(
        ["targets"], _make_ctx(tmp_path), allow_targets=["read.*"]
    )
    assert [s.name for s in executor.tool_specs()] == ["read_code", "read_git"]


@pytest.mark.asyncio
async def test_execute_tool_dispatches_sync_and_async(tmp_path: Path) -> None:
    async def async_handler(args: dict) -> dict:
        return {"via": "async", **args}

    def sync_handler(args: dict) -> dict:
        return {"via": "sync", **args}

    reg = BundleRegistry()
    reg.register(
        "handlers",
        lambda ctx: [
            (_spec("a"), async_handler),
            (_spec("s"), sync_handler),
        ],
    )
    executor = reg.build(["handlers"], _make_ctx(tmp_path))

    a_result = await executor.execute_tool("a", {"x": 1})
    s_result = await executor.execute_tool("s", {"y": 2})
    assert a_result == {"via": "async", "x": 1}
    assert s_result == {"via": "sync", "y": 2}


@pytest.mark.asyncio
async def test_execute_tool_unknown_returns_structured_error(tmp_path: Path) -> None:
    reg = BundleRegistry()
    reg.register(
        "only_a", lambda ctx: [(_spec("a"), lambda args: {"ok": True})]
    )
    executor = reg.build(["only_a"], _make_ctx(tmp_path))
    result = await executor.execute_tool("missing", {})
    assert isinstance(result, dict)
    assert result["error"].startswith("TOOL_NOT_REGISTERED")
    assert result["registered"] == ["a"]


def test_context_forwarded_to_factories(tmp_path: Path) -> None:
    """Factories must receive the exact BundleContext passed to build()."""
    captured: list[BundleContext] = []

    def factory(ctx: BundleContext) -> list:
        captured.append(ctx)
        return [(_spec("noop"), lambda a: None)]

    reg = BundleRegistry()
    reg.register("cap", factory)
    ctx = _make_ctx(tmp_path)
    reg.build(["cap"], ctx)
    assert captured == [ctx]
