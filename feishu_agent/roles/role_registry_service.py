from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml


@dataclass
class RoleDefinition:
    """Role metadata parsed from ``skills/roles/<role>.md`` frontmatter.

    ``tool_allow_list`` is the *legacy* override (strict explicit list).
    When non-empty it wins outright — any tool not listed is filtered.

    ``allow_effects`` / ``allow_targets`` are the M3 policy fields
    introduced together with the decorator-style tool registry. They
    are *additive* over ``tool_allow_list``: when ``tool_allow_list``
    is empty the role sees exactly the tools whose ``effect`` is in
    ``allow_effects`` AND whose ``target`` matches one of
    ``allow_targets`` as an fnmatch glob. Self-state tools should
    usually be permitted via ``allow_effects=["self", ...]`` rather
    than being enumerated by name.

    ``tool_bundles`` (004 A-2) is the declarative tool surface used by
    :class:`feishu_agent.roles.generic_role_executor.GenericRoleExecutor`:
    each entry names a bundle under
    :mod:`feishu_agent.tools.bundles`. Composition order is the role's
    declared order; duplicates across bundles raise at build time
    (see ``ToolNameCollisionError``).

    ``concurrency_group`` / ``needs_worktree`` / ``worktree_base_branch``
    (004 B-2 / B-3) are consumed by the TechLead dispatch path at
    fan-out and worktree acquisition time respectively.
    """

    role_name: str
    tags: list[str] = field(default_factory=list)
    tool_allow_list: list[str] = field(default_factory=list)
    allow_effects: list[str] = field(default_factory=list)
    allow_targets: list[str] = field(default_factory=list)
    tool_bundles: list[str] = field(default_factory=list)
    concurrency_group: str | None = None
    needs_worktree: bool = False
    worktree_base_branch: str = "main"
    model: str | None = None
    system_prompt: str = ""


class RoleNotFoundError(Exception):
    pass


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)", re.DOTALL)


class RoleRegistryService:
    def __init__(self, roles_dir: Path) -> None:
        self.roles_dir = roles_dir
        self._executor_factories: dict[str, Callable[..., Any]] = {}

    def get_role(self, role_name: str) -> RoleDefinition:
        path = self.roles_dir / f"{role_name}.md"
        if not path.exists():
            raise RoleNotFoundError(f"Role file not found: {path}")
        return self._parse_role_file(role_name, path)

    def list_roles(self, tag: str | None = None) -> list[RoleDefinition]:
        if not self.roles_dir.exists():
            return []
        roles: list[RoleDefinition] = []
        for path in sorted(self.roles_dir.glob("*.md")):
            role = self._parse_role_file(path.stem, path)
            if tag is None or tag in role.tags:
                roles.append(role)
        return roles

    def register_executor_factory(self, role_name: str, factory: Callable[..., Any]) -> None:
        self._executor_factories[role_name] = factory

    def get_executor_factory(self, role_name: str) -> Callable[..., Any] | None:
        return self._executor_factories.get(role_name)

    def _parse_role_file(self, role_name: str, path: Path) -> RoleDefinition:
        text = path.read_text(encoding="utf-8")
        match = _FRONTMATTER_RE.match(text)
        if not match:
            return RoleDefinition(role_name=role_name, system_prompt=text.strip())

        frontmatter_raw = match.group(1)
        body = match.group(2).strip()
        try:
            parsed = yaml.safe_load(frontmatter_raw) or {}
        except yaml.YAMLError:
            return RoleDefinition(role_name=role_name, system_prompt=text.strip())

        concurrency_group_raw = parsed.get("concurrency_group")
        if isinstance(concurrency_group_raw, str):
            concurrency_group: str | None = concurrency_group_raw or None
        else:
            concurrency_group = None

        base_branch_raw = parsed.get("worktree_base_branch")
        worktree_base_branch = (
            base_branch_raw if isinstance(base_branch_raw, str) and base_branch_raw else "main"
        )

        return RoleDefinition(
            role_name=role_name,
            tags=parsed.get("tags") or [],
            tool_allow_list=parsed.get("tool_allow_list") or [],
            allow_effects=list(parsed.get("allow_effects") or []),
            allow_targets=list(parsed.get("allow_targets") or []),
            tool_bundles=list(parsed.get("tool_bundles") or []),
            concurrency_group=concurrency_group,
            needs_worktree=_coerce_bool_flag(parsed.get("needs_worktree")),
            worktree_base_branch=worktree_base_branch,
            model=parsed.get("model"),
            system_prompt=body,
        )


# YAML boolean literals resolve to Python ``bool`` via ``yaml.safe_load``.
# Quoted forms ("true" / "false") come back as ``str`` — Python's default
# ``bool()`` coerces any non-empty string to True, which would silently flip
# ``needs_worktree: "false"`` into True and enable worktree isolation when
# the author meant to disable it. This coercer is strict about the common
# quoted forms and falls back to ``False`` for anything else, so author
# typos surface as "not enabled" instead of "surprisingly enabled".
_TRUTHY_FLAG_STRINGS = frozenset({"true", "yes", "on", "1"})
_FALSY_FLAG_STRINGS = frozenset({"false", "no", "off", "0", ""})


def _coerce_bool_flag(value: Any) -> bool:
    """Coerce a YAML frontmatter scalar into a strict boolean.

    Accepts real booleans, the common YAML truthy/falsy keywords (quoted or
    not), and 0 / 1. Anything else (including ``None`` and unknown strings)
    returns False so misconfigured frontmatter fails safe rather than
    silently enabling a feature flag.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUTHY_FLAG_STRINGS:
            return True
        if normalized in _FALSY_FLAG_STRINGS:
            return False
    return False
