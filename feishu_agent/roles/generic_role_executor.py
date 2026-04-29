"""Generic role executor — composes a tool surface from a RoleDefinition.

Replaces the per-role Python executor classes (``RepoInspectorExecutor``,
``ResearcherExecutor`` …) for any role whose frontmatter declares
``tool_bundles: [...]``. Those class-based executors were ~90% tool-spec
boilerplate; with A-2 the surface is authored declaratively and this
class does the composition.

Semantics
---------
- Tools come from the union of :class:`BundleRegistry` bundles named by
  the role.
- Filtering is applied in the registry's :meth:`BundleRegistry.build`:
  ``allow_effects`` then ``allow_targets`` (both optional).
- If ``role.tool_allow_list`` is also set (legacy 002 roles), the
  resulting surface is further restricted to that whitelist.
- Dispatch is O(1) via the composite executor's handler dict.

Stateless — the registry is passed in; construction is cheap so TL can
build a fresh executor per dispatch without caching.
"""

from __future__ import annotations

from typing import Any

from feishu_agent.core.agent_types import (
    AgentToolExecutor,
    AgentToolSpec,
    AllowListedToolExecutor,
)
from feishu_agent.roles.role_registry_service import RoleDefinition
from feishu_agent.tools.bundle_context import BundleContext
from feishu_agent.tools.bundle_registry import BundleRegistry


class GenericRoleExecutor:
    """Composite :class:`AgentToolExecutor` for bundle-declared roles."""

    def __init__(
        self,
        role: RoleDefinition,
        registry: BundleRegistry,
        ctx: BundleContext,
    ) -> None:
        self._role = role
        composite: AgentToolExecutor = registry.build(
            role.tool_bundles,
            ctx,
            allow_effects=role.allow_effects or None,
            allow_targets=role.allow_targets or None,
        )
        # If a role still declares an explicit allow-list (legacy 002
        # pattern), honor it as an additional filter on top of bundle
        # composition. When empty, this is a pass-through.
        if role.tool_allow_list:
            self._executor: AgentToolExecutor = AllowListedToolExecutor(
                composite, role.tool_allow_list
            )
        else:
            self._executor = composite

    @property
    def role(self) -> RoleDefinition:
        return self._role

    def tool_specs(self) -> list[AgentToolSpec]:
        return self._executor.tool_specs()

    async def execute_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | list[Any] | str:
        return await self._executor.execute_tool(tool_name, arguments)
