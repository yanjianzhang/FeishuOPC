"""Module-scan tool and role registry.

Motivation
----------
Before this module, adding a new tool required two edits:
1. Write a method on a mixin / executor class.
2. Register it in a central table (``role_executors/__init__.py``
   for roles, ``WorkflowToolsMixin`` for workflow tools, etc.).

This violates open/closed: every addition poked the central
table. The plan called for "decorator + module scan" — drop a file
in ``feishu_agent/tools/legacy_tools/<topic>.py`` (or a role class
with ``@role``), and ``autodiscover()`` picks it up.

The registry does **not** replace :class:`AgentToolExecutor` —
decorator-registered tools are bundled into an executor at load
time, so the adapter surface is unchanged.

Contracts
---------
- ``@tool(...)`` wraps an ``async`` function and registers it into
  :data:`GLOBAL_TOOL_REGISTRY`. The function signature must start
  with the LLM-visible arguments (matching ``input_schema``); any
  parameter named in ``needs=(…)`` is injected by the adapter at
  call time, NOT exposed to the model.
- ``@role(name=…, allow_effects=…, allow_targets=…)`` tags a class
  as a role executor; the registry records the class plus any
  role-level allow policies.
- ``autodiscover(packages=[…])`` imports every submodule under the
  given packages so decorators fire. Idempotent.

Non-goals
---------
- No dependency injection container. Tools declare ``needs``; the
  adapter supplies them from a plain dict. Hermes-style.
- No RPC / remote tool support. Registry is in-process only.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from feishu_agent.core.agent_types import AgentToolExecutor, AgentToolSpec

logger = logging.getLogger(__name__)


ToolFn = Callable[..., Awaitable[Any]]


@dataclass
class ToolEntry:
    """Registry row for one ``@tool``-decorated function."""

    spec: AgentToolSpec
    fn: ToolFn
    module: str


@dataclass
class RoleEntry:
    """Registry row for one ``@role``-decorated executor class.

    ``tool_allow_list`` is stored here for forward compatibility but
    is NOT consulted by :class:`ToolPolicy` today. The runtime reads
    the effective allow-list from :attr:`RoleDefinition.tool_allow_list`
    (parsed from the role's md frontmatter), because that's the source
    of truth for already-migrated roles. When a role class goes fully
    code-native (no md), wiring :meth:`RoleEntry.tool_allow_list` into
    :class:`ToolPolicy` is a one-line change. Until then, treat this
    attribute as an annotation, not a filter.
    """

    name: str
    cls: type
    allow_effects: tuple[str, ...] = ()
    allow_targets: tuple[str, ...] = ()
    tool_allow_list: tuple[str, ...] = ()  # reserved; see class docstring


@dataclass
class ToolRegistry:
    """In-memory store for tools + roles. Usually used via the module
    singleton :data:`GLOBAL_TOOL_REGISTRY`, but tests can construct a
    throwaway instance for isolation."""

    tools: dict[str, ToolEntry] = field(default_factory=dict)
    roles: dict[str, RoleEntry] = field(default_factory=dict)

    def register_tool(self, entry: ToolEntry) -> None:
        if entry.spec.name in self.tools:
            # Duplicate registration is almost always a wiring bug —
            # a module imported twice because two packages list it.
            logger.warning(
                "tool %r re-registered from %s (was %s); keeping later",
                entry.spec.name,
                entry.module,
                self.tools[entry.spec.name].module,
            )
        self.tools[entry.spec.name] = entry

    def register_role(self, entry: RoleEntry) -> None:
        if entry.name in self.roles:
            logger.warning("role %r re-registered; keeping later", entry.name)
        self.roles[entry.name] = entry

    def get_tool(self, name: str) -> ToolEntry | None:
        return self.tools.get(name)

    def list_tools(self) -> list[ToolEntry]:
        return list(self.tools.values())

    def list_roles(self) -> list[RoleEntry]:
        return list(self.roles.values())

    # ------------------------------------------------------------------
    # Executor adapter — expose a subset of registered tools as an
    # :class:`AgentToolExecutor` suitable for the LLM adapter.
    # ------------------------------------------------------------------

    def build_executor(
        self,
        *,
        tool_names: Iterable[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> AgentToolExecutor:
        """Build an executor over a subset of registered tools.

        ``tool_names`` — if ``None`` all tools are exposed; otherwise
        only the named ones. The adapter's allow-list filtering is a
        separate concern (see :class:`AllowListedToolExecutor`).

        ``context`` — the pool of runtime values the registry uses to
        satisfy each tool's ``needs``. Missing values are replaced with
        ``None``; the tool is responsible for handling that. Unknown
        keys in ``context`` are ignored.
        """
        names_filter = set(tool_names) if tool_names else None
        selected = [
            entry
            for entry in self.tools.values()
            if names_filter is None or entry.spec.name in names_filter
        ]
        return _RegistryExecutor(selected, context or {})


GLOBAL_TOOL_REGISTRY = ToolRegistry()


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------


def tool(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any],
    effect: str = "world",
    target: str = "*",
    needs: Iterable[str] = (),
    registry: ToolRegistry | None = None,
) -> Callable[[ToolFn], ToolFn]:
    """Register an async function as a tool.

    The wrapped function's signature is preserved. At runtime the
    adapter calls ``fn(**llm_arguments, **{k: context[k] for k in needs})``;
    parameters listed in ``needs`` are NOT validated against the
    ``input_schema`` and NOT sent to the LLM.
    """

    def _decorator(fn: ToolFn) -> ToolFn:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"@tool '{name}' must decorate an async function")
        spec = AgentToolSpec(
            name=name,
            description=description,
            input_schema=input_schema,
            effect=effect,
            target=target,
            needs=tuple(needs),
        )
        entry = ToolEntry(spec=spec, fn=fn, module=fn.__module__)
        (registry or GLOBAL_TOOL_REGISTRY).register_tool(entry)
        fn.__agent_tool_spec__ = spec  # type: ignore[attr-defined]
        return fn

    return _decorator


def role(
    *,
    name: str,
    allow_effects: Iterable[str] = (),
    allow_targets: Iterable[str] = (),
    tool_allow_list: Iterable[str] = (),
    registry: ToolRegistry | None = None,
) -> Callable[[type], type]:
    """Register an executor class as a named role.

    ``allow_effects`` / ``allow_targets`` are the M3 replacement for
    the ad-hoc ``_WRITE_ONLY_TOOL_NAMES`` frozenset — a role is
    allowed to invoke a tool iff:

        tool.effect in allow_effects  AND  fnmatch(tool.target, any allow_targets)

    ``tool_allow_list`` (frontmatter override from the role's md file)
    still wins — if present it is a strict allow-list over the union.
    """

    def _decorator(cls: type) -> type:
        entry = RoleEntry(
            name=name,
            cls=cls,
            allow_effects=tuple(allow_effects),
            allow_targets=tuple(allow_targets),
            tool_allow_list=tuple(tool_allow_list),
        )
        (registry or GLOBAL_TOOL_REGISTRY).register_role(entry)
        cls.__agent_role_entry__ = entry  # type: ignore[attr-defined]
        return cls

    return _decorator


# ---------------------------------------------------------------------------
# Module scan
# ---------------------------------------------------------------------------


def autodiscover(
    packages: Iterable[str],
    *,
    registry: ToolRegistry | None = None,  # noqa: ARG001 — kept for symmetry
) -> list[str]:
    """Import every submodule under ``packages`` so decorators fire.

    Returns the list of imported module names, mostly so tests can
    assert the expected surface was scanned. Idempotent — importing
    an already-imported module is a no-op and decorators dedupe.

    We deliberately don't swallow import errors: a broken module is
    a wiring bug operators must see at startup, not a silent miss.
    """
    imported: list[str] = []
    for pkg_name in packages:
        try:
            pkg = importlib.import_module(pkg_name)
        except ImportError:
            logger.exception("autodiscover: failed to import package %s", pkg_name)
            continue
        if not hasattr(pkg, "__path__"):
            imported.append(pkg_name)
            continue
        for module_info in pkgutil.walk_packages(pkg.__path__, pkg_name + "."):
            mod_name = module_info.name
            if any(part.startswith("_") for part in mod_name.split(".")):
                # Skip ``_private`` subpackages / test modules; the
                # convention ``_tests`` is common for in-place tests.
                continue
            try:
                importlib.import_module(mod_name)
                imported.append(mod_name)
            except ImportError:
                logger.exception("autodiscover: failed to import %s", mod_name)
    return imported


# ---------------------------------------------------------------------------
# Executor implementation
# ---------------------------------------------------------------------------


class _RegistryExecutor:
    """AgentToolExecutor wrapping a list of :class:`ToolEntry`.

    Handles the per-call context injection: when executing a tool
    whose spec declares ``needs=("task_id", "chat_id")``, the executor
    merges those keys from its context dict into the tool's kwargs.
    The LLM never sees them.
    """

    def __init__(self, entries: list[ToolEntry], context: dict[str, Any]) -> None:
        self._by_name = {e.spec.name: e for e in entries}
        self._context = dict(context)

    def tool_specs(self) -> list[AgentToolSpec]:
        return [entry.spec for entry in self._by_name.values()]

    async def execute_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | list[Any] | str:
        entry = self._by_name.get(tool_name)
        if entry is None:
            return {"ok": False, "error": f"UNKNOWN_TOOL: {tool_name}"}
        # Strip any LLM-supplied keys that collide with ``needs`` names
        # before merging. The LLM must NOT be able to shadow a runtime-
        # injected context parameter (e.g. forging ``task_handle`` or
        # ``allowed_write_root``). Without this filter, a colliding
        # argument triggers ``TypeError: multiple values`` which is
        # caught below as ``INVALID_ARGUMENTS`` — effectively letting a
        # confused LLM disable injection. We drop the LLM key instead.
        needs = entry.spec.needs
        if needs:
            safe_arguments = {k: v for k, v in arguments.items() if k not in needs}
        else:
            safe_arguments = arguments
        injected = {k: self._context.get(k) for k in needs}
        try:
            return await entry.fn(**safe_arguments, **injected)
        except TypeError as exc:
            # Usually an arguments-vs-schema mismatch. Surface it as a
            # structured error so the LLM can correct and retry.
            return {"ok": False, "error": "INVALID_ARGUMENTS", "detail": str(exc)}


__all__ = [
    "ToolRegistry",
    "ToolEntry",
    "RoleEntry",
    "GLOBAL_TOOL_REGISTRY",
    "tool",
    "role",
    "autodiscover",
]
