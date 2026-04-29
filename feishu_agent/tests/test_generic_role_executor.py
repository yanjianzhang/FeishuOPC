"""Unit tests for :class:`GenericRoleExecutor`.

Validates the A-2 contract:
- ``tool_specs()`` returns the union of bundles declared by the role.
- ``execute_tool`` routes to the right handler.
- ``allow_effects`` / ``allow_targets`` from the role flow into the
  registry filter.
- A non-empty ``tool_allow_list`` further restricts the surface.
- Missing bundles surface as :class:`BundleNotFoundError`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.roles.generic_role_executor import GenericRoleExecutor
from feishu_agent.roles.role_registry_service import RoleDefinition
from feishu_agent.tools.bundle_context import BundleContext
from feishu_agent.tools.bundle_registry import BundleNotFoundError, BundleRegistry


def _spec(name: str, effect: str = "read", target: str = "*") -> AgentToolSpec:
    return AgentToolSpec(
        name=name,
        description=f"Test {name}",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effect=effect,
        target=target,
    )


def _ctx(tmp_path: Path) -> BundleContext:
    return BundleContext(
        working_dir=tmp_path,
        repo_root=tmp_path,
        chat_id="c",
        trace_id="t",
        role_name="role",
    )


def _registry_with_two_bundles() -> BundleRegistry:
    reg = BundleRegistry()
    reg.register(
        "fs_read",
        lambda ctx: [
            (_spec("read_project_code", effect="read", target="read.fs"), lambda a: {"src": "ok"}),
            (_spec("list_project_paths", effect="read", target="read.fs"), lambda a: {"paths": []}),
        ],
    )
    reg.register(
        "sprint",
        lambda ctx: [
            (_spec("read_sprint_status", effect="read", target="read.sprint"), lambda a: {"sprint": "alpha"}),
            (_spec("advance_sprint_state", effect="world", target="world.sprint"), lambda a: {"advanced": True}),
        ],
    )
    return reg


def test_tool_specs_union_of_bundles(tmp_path: Path) -> None:
    reg = _registry_with_two_bundles()
    role = RoleDefinition(
        role_name="repo_inspector",
        tool_bundles=["fs_read", "sprint"],
        allow_effects=["read"],
    )
    ex = GenericRoleExecutor(role, reg, _ctx(tmp_path))
    names = [s.name for s in ex.tool_specs()]
    # world-effect advance_sprint_state filtered out by allow_effects=["read"].
    assert names == ["read_project_code", "list_project_paths", "read_sprint_status"]


@pytest.mark.asyncio
async def test_execute_tool_routes_to_bundle_handler(tmp_path: Path) -> None:
    reg = _registry_with_two_bundles()
    role = RoleDefinition(
        role_name="repo_inspector",
        tool_bundles=["fs_read", "sprint"],
        allow_effects=["read"],
    )
    ex = GenericRoleExecutor(role, reg, _ctx(tmp_path))
    assert await ex.execute_tool("read_project_code", {}) == {"src": "ok"}
    assert await ex.execute_tool("read_sprint_status", {}) == {"sprint": "alpha"}


def test_tool_allow_list_further_restricts_surface(tmp_path: Path) -> None:
    reg = _registry_with_two_bundles()
    role = RoleDefinition(
        role_name="narrow",
        tool_bundles=["fs_read", "sprint"],
        allow_effects=["read", "world"],
        tool_allow_list=["read_sprint_status"],
    )
    ex = GenericRoleExecutor(role, reg, _ctx(tmp_path))
    names = [s.name for s in ex.tool_specs()]
    assert names == ["read_sprint_status"]


def test_allow_targets_glob_flows_through(tmp_path: Path) -> None:
    reg = _registry_with_two_bundles()
    role = RoleDefinition(
        role_name="only_fs",
        tool_bundles=["fs_read", "sprint"],
        allow_effects=["read"],
        allow_targets=["read.fs"],
    )
    ex = GenericRoleExecutor(role, reg, _ctx(tmp_path))
    names = [s.name for s in ex.tool_specs()]
    assert names == ["read_project_code", "list_project_paths"]


def test_unknown_bundle_raises(tmp_path: Path) -> None:
    reg = _registry_with_two_bundles()
    role = RoleDefinition(
        role_name="typo",
        tool_bundles=["fs_redd"],  # typo
    )
    with pytest.raises(BundleNotFoundError) as excinfo:
        GenericRoleExecutor(role, reg, _ctx(tmp_path))
    assert "fs_redd" in str(excinfo.value)
