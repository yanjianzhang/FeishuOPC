"""Declarative tool allow-policy: ``allow_effects`` + ``allow_targets``.

Background
----------
Historically each role limited which tools it could invoke via an ad-
hoc ``frozenset[str]`` (``DEVELOPER_CODE_WRITE_ALLOW``,
``TECH_LEAD_CODE_WRITE_ALLOW``, ``REVIEWER_CODE_READ_ALLOW``, plus the
``_WRITE_ONLY_TOOL_NAMES`` denylist in ``workflow_tools``). Every new
sensitive tool meant editing one of those sets — and occasionally a
role's frontmatter — to grant access. That's O(N*M) edits.

M3 replaces the enumerated lists with two declarative fields on
``RoleDefinition``:

- ``allow_effects`` — the categorical axis: e.g. ``["self", "read"]``.
- ``allow_targets`` — the scope axis, fnmatch globs over
  ``AgentToolSpec.target``: e.g. ``["self.*", "world.git.read_*"]``.

The rule is conjunctive: a tool is allowed iff its ``effect`` is in
``allow_effects`` AND its ``target`` matches *any* entry in
``allow_targets``.

Backwards compatibility
-----------------------
- ``tool_allow_list`` (role md frontmatter) is still honored and,
  when non-empty, takes priority: a role with an explicit allow-list
  sees exactly those tool names and nothing else. This is the
  "break-glass" override for weird cases.
- A tool whose spec has ``effect="world"`` and ``target="*"`` — the
  backwards-compat default the :class:`AgentToolSpec` dataclass
  produces — remains allowed by any role that lists ``"world"`` in
  ``allow_effects`` (so legacy mixin tools aren't silently banned).

Why fnmatch
-----------
Globs are simple, intuitive, and readable in a YAML frontmatter
(``world.git.read_*``). We deliberately avoid regex — roles should
not be asked to understand anchoring, escape rules, or
non-greediness. If a role truly needs a tool it can either list the
name explicitly or widen the glob.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from typing import Any, Iterable

from feishu_agent.core.agent_types import AgentToolExecutor, AgentToolSpec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolPolicy:
    """Role-level declarative policy."""

    allow_effects: frozenset[str]
    allow_targets: tuple[str, ...]
    tool_allow_list: frozenset[str]  # strict override; empty means "not used"

    @classmethod
    def from_role(
        cls,
        *,
        allow_effects: Iterable[str] = (),
        allow_targets: Iterable[str] = (),
        tool_allow_list: Iterable[str] = (),
    ) -> "ToolPolicy":
        return cls(
            allow_effects=frozenset(allow_effects),
            allow_targets=tuple(allow_targets),
            tool_allow_list=frozenset(tool_allow_list),
        )

    def is_empty(self) -> bool:
        """A policy with no fields set permits everything (legacy)."""
        return (
            not self.allow_effects
            and not self.allow_targets
            and not self.tool_allow_list
        )

    def permits(self, spec: AgentToolSpec) -> bool:
        """Return True if ``spec`` is allowed under this policy."""
        # The explicit allow-list (md frontmatter) wins outright.
        if self.tool_allow_list:
            return spec.name in self.tool_allow_list

        # No declarative policy specified → default-allow. This keeps
        # legacy roles working while the world is migrated.
        if not self.allow_effects and not self.allow_targets:
            return True

        # Effect filter. An unset allow_effects behaves as "any".
        if self.allow_effects and spec.effect not in self.allow_effects:
            return False

        # Target filter. An unset allow_targets behaves as "any"; a
        # non-empty list requires at least one glob match.
        if self.allow_targets and not _matches_any(spec.target, self.allow_targets):
            return False

        return True


def _matches_any(target: str, patterns: Iterable[str]) -> bool:
    """``fnmatch`` helper — True iff ``target`` matches any pattern."""
    return any(fnmatch.fnmatchcase(target, pattern) for pattern in patterns)


class PolicyFilteredExecutor:
    """Wrap an :class:`AgentToolExecutor`, filter by :class:`ToolPolicy`.

    Behavior:

    - ``tool_specs()`` returns only specs the policy permits. This is
      what the LLM sees — refused tools are invisible, not "there but
      forbidden", because hiding them reduces hallucinated calls.
    - ``execute_tool()`` rejects a name the policy would not have
      exposed with a structured error (in case a stale call slips
      through tool-schema caching on the LLM side).

    The wrapper is a thin layer over any existing executor; it
    composes cleanly under ``CombinedExecutor`` (wrap the world
    executor, then combine with ``TaskStateExecutor``) or alongside
    :class:`AllowListedToolExecutor` (which keeps its own
    name-based allow-list as the legacy override path).
    """

    def __init__(
        self, inner: AgentToolExecutor, policy: ToolPolicy
    ) -> None:
        self._inner = inner
        self._policy = policy
        # Cache the permitted names so ``execute_tool`` can reject
        # without re-running the policy check on every turn. Specs
        # are stable across a session — if they change, we'd need to
        # invalidate, but no current executor mutates mid-session.
        self._permitted: set[str] = {
            spec.name
            for spec in inner.tool_specs()
            if policy.permits(spec)
        }

    # ------------------------------------------------------------------
    # AgentToolExecutor protocol
    # ------------------------------------------------------------------

    def tool_specs(self) -> list[AgentToolSpec]:
        return [
            spec for spec in self._inner.tool_specs() if spec.name in self._permitted
        ]

    async def execute_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | list[Any] | str:
        if tool_name not in self._permitted:
            return {
                "ok": False,
                "error": "TOOL_NOT_ALLOWED_BY_POLICY",
                "tool": tool_name,
                "allowed": sorted(self._permitted),
            }
        return await self._inner.execute_tool(tool_name, arguments)


__all__ = ["ToolPolicy", "PolicyFilteredExecutor"]
