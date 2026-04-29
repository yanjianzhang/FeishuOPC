"""Tool-bundle registry and composite executor.

Bundles are the 004-scalable-agent-foundation replacement for the
per-role Python executor classes. Each bundle is one factory module
under :mod:`feishu_agent.tools.bundles` that returns a list of
``(AgentToolSpec, handler)`` pairs. A role's tool surface is the union
of its :attr:`RoleDefinition.tool_bundles`, optionally filtered by
``allow_effects`` / ``allow_targets``.

Design notes
------------
- Factories are invoked *per dispatch*; we never cache the resulting
  :class:`_CompositeExecutor`. This keeps ``BundleContext`` per-request
  services fresh and is how B-3 wires a different ``working_dir``
  (worktree path) into otherwise identical bundles.
- Name collisions across bundles fail **loudly**: same-name tools are a
  configuration error (ambiguous role surface), not a silent last-wins.
- Unknown bundle names fail **loudly** with the current registry listed
  so misspellings show up as an actionable error instead of "this role
  has no tools".
"""

from __future__ import annotations

from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, Callable

from feishu_agent.core.agent_types import AgentToolExecutor, AgentToolSpec
from feishu_agent.tools.bundle_context import BundleContext

Handler = Callable[[dict[str, Any]], Awaitable[Any] | Any]
BundleFactory = Callable[[BundleContext], list[tuple[AgentToolSpec, Handler]]]


class BundleNotFoundError(KeyError):
    """Raised when a role references a bundle name that isn't registered."""

    def __init__(self, name: str, known: Sequence[str]) -> None:
        self.name = name
        self.known = sorted(known)
        super().__init__(
            f"Bundle not registered: {name!r}. Known bundles: {self.known}"
        )


class ToolNameCollisionError(ValueError):
    """Raised when two bundles export the same tool name for one role."""

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        super().__init__(
            f"Tool name collision across bundles: {tool_name!r}. "
            "Rename one of them or split the role so only one bundle "
            "exposes the tool."
        )


@dataclass
class _CompositeExecutor:
    """Composite :class:`AgentToolExecutor` built from bundle output.

    Stores specs in a stable list (so ``tool_specs()`` preserves
    bundle order) and a name→handler dict (so dispatch is O(1)).
    """

    _specs: list[AgentToolSpec]
    _handlers: dict[str, Handler]

    def tool_specs(self) -> list[AgentToolSpec]:
        return list(self._specs)

    async def execute_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | list[Any] | str:
        handler = self._handlers.get(tool_name)
        if handler is None:
            return {
                "error": f"TOOL_NOT_REGISTERED: {tool_name}",
                "registered": sorted(self._handlers),
            }
        result = handler(arguments)
        if isinstance(result, Awaitable):
            result = await result
        return result


def _target_matches(target: str, patterns: Sequence[str]) -> bool:
    """fnmatch-based glob, **case-sensitive**.

    Chosen to match the M3 allow-policy glob semantics already in use
    throughout :class:`feishu_agent.roles.role_registry_service.RoleDefinition`
    consumers. Targets and ``allow_targets`` patterns MUST share a case
    convention (lowercase, dot-separated is the house style — e.g.
    ``"read.fs"``, ``"world.git.remote"``); a capitalised target like
    ``"READ.fs"`` will silently fail to match ``"read.*"`` and be
    filtered out of the role surface.
    """
    return any(fnmatchcase(target, pat) for pat in patterns)


class BundleRegistry:
    """Process-level registry of bundle factories.

    Build a registry once at startup (``register("fs_read",
    build_fs_read_bundle)`` etc.) and hand it to
    :class:`GenericRoleExecutor` per dispatch. The registry is
    immutable-after-setup in spirit — :meth:`register` refuses
    duplicate names so hot reloads must use a fresh registry.
    """

    def __init__(self) -> None:
        self._factories: dict[str, BundleFactory] = {}

    def register(self, name: str, factory: BundleFactory) -> None:
        if name in self._factories:
            raise ValueError(f"Bundle already registered: {name}")
        self._factories[name] = factory

    def known_bundles(self) -> list[str]:
        return sorted(self._factories)

    def build(
        self,
        bundle_names: Sequence[str],
        ctx: BundleContext,
        *,
        allow_effects: Sequence[str] | None = None,
        allow_targets: Sequence[str] | None = None,
    ) -> AgentToolExecutor:
        items: list[tuple[AgentToolSpec, Handler]] = []
        for name in bundle_names:
            factory = self._factories.get(name)
            if factory is None:
                raise BundleNotFoundError(name, self._factories)
            items.extend(factory(ctx))

        if allow_effects:
            effect_set = set(allow_effects)
            items = [(s, h) for (s, h) in items if s.effect in effect_set]

        if allow_targets:
            items = [
                (s, h) for (s, h) in items if _target_matches(s.target, allow_targets)
            ]

        specs: list[AgentToolSpec] = []
        handlers: dict[str, Handler] = {}
        for spec, handler in items:
            if spec.name in handlers:
                raise ToolNameCollisionError(spec.name)
            specs.append(spec)
            handlers[spec.name] = handler

        return _CompositeExecutor(_specs=specs, _handlers=handlers)
