"""Tests for :class:`ToolPolicy` and :class:`PolicyFilteredExecutor`."""

from __future__ import annotations

import asyncio
from typing import Any

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.core.tool_policy import PolicyFilteredExecutor, ToolPolicy


def _spec(name: str, effect: str = "world", target: str = "*") -> AgentToolSpec:
    return AgentToolSpec(
        name=name,
        description=name,
        input_schema={"type": "object"},
        effect=effect,
        target=target,
    )


class _Stub:
    def __init__(self, specs: list[AgentToolSpec]) -> None:
        self._specs = specs
        self.calls: list[str] = []

    def tool_specs(self) -> list[AgentToolSpec]:
        return list(self._specs)

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]):
        self.calls.append(tool_name)
        return {"ok": True, "name": tool_name}


# ---------------------------------------------------------------------------
# ToolPolicy.permits
# ---------------------------------------------------------------------------


def test_empty_policy_permits_everything():
    policy = ToolPolicy.from_role()
    assert policy.is_empty()
    for eff in ("self", "world", "read", "weird"):
        assert policy.permits(_spec(f"x.{eff}", effect=eff))


def test_effect_only_policy_filters_by_effect():
    policy = ToolPolicy.from_role(allow_effects=["self", "read"])
    assert policy.permits(_spec("x", effect="self", target="self.plan"))
    assert policy.permits(_spec("x", effect="read"))
    assert not policy.permits(_spec("x", effect="world"))


def test_target_glob_policy_filters_by_target():
    policy = ToolPolicy.from_role(
        allow_effects=["self", "read", "world"],
        allow_targets=["self.*", "world.git.read_*"],
    )
    assert policy.permits(_spec("x", effect="self", target="self.plan"))
    assert policy.permits(_spec("x", effect="world", target="world.git.read_status"))
    assert not policy.permits(_spec("x", effect="world", target="world.git.push"))
    assert not policy.permits(_spec("x", effect="world", target="world.feishu.reply"))


def test_explicit_allow_list_wins_over_effects_and_targets():
    policy = ToolPolicy.from_role(
        allow_effects=["self"],
        allow_targets=["self.*"],
        tool_allow_list=["x"],
    )
    # x is on the allow-list even though effect is "world"
    assert policy.permits(_spec("x", effect="world", target="world.git.push"))
    # y is not on the list; filtered regardless of effects/targets
    assert not policy.permits(_spec("y", effect="self", target="self.plan"))


def test_legacy_tool_with_default_target_star_matches_star_patterns():
    # A tool created before M3 has effect='world' and target='*'.
    # A role that lists allow_targets=['*'] should still see it.
    policy = ToolPolicy.from_role(
        allow_effects=["world"],
        allow_targets=["*"],
    )
    assert policy.permits(_spec("legacy"))  # target='*', effect='world'


# ---------------------------------------------------------------------------
# PolicyFilteredExecutor
# ---------------------------------------------------------------------------


def test_executor_hides_and_rejects_filtered_tools():
    stub = _Stub(
        [
            _spec("set_mode", effect="self", target="self.mode"),
            _spec("push", effect="world", target="world.git.push"),
            _spec("status", effect="read", target="world.git.read_status"),
        ]
    )
    policy = ToolPolicy.from_role(
        allow_effects=["self", "read"],
        allow_targets=["self.*", "world.git.read_*"],
    )
    executor = PolicyFilteredExecutor(stub, policy)

    names = [s.name for s in executor.tool_specs()]
    assert names == ["set_mode", "status"]

    ok = asyncio.run(executor.execute_tool("status", {}))
    assert ok == {"ok": True, "name": "status"}

    refused = asyncio.run(executor.execute_tool("push", {}))
    assert refused == {
        "ok": False,
        "error": "TOOL_NOT_ALLOWED_BY_POLICY",
        "tool": "push",
        "allowed": ["set_mode", "status"],
    }
    assert "push" not in stub.calls


def test_empty_policy_is_transparent_passthrough():
    stub = _Stub([_spec("anything")])
    executor = PolicyFilteredExecutor(stub, ToolPolicy.from_role())
    assert [s.name for s in executor.tool_specs()] == ["anything"]
    assert asyncio.run(executor.execute_tool("anything", {})) == {
        "ok": True,
        "name": "anything",
    }
