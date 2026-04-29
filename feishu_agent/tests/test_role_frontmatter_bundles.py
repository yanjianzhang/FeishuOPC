"""Tests covering the 004 A-2 frontmatter extensions on ``RoleDefinition``.

Specifically:
- ``tool_bundles`` is parsed as a list.
- ``concurrency_group`` keeps ``None`` unless a non-empty string is given.
- ``needs_worktree`` coerces truthy/falsy YAML.
- ``worktree_base_branch`` defaults to ``"main"`` when missing or empty.
- Legacy roles without the new keys still parse cleanly with old defaults.
"""

from __future__ import annotations

from pathlib import Path

from feishu_agent.roles.role_registry_service import (
    RoleDefinition,
    RoleRegistryService,
)


def _write_role(roles_dir: Path, role_name: str, frontmatter: str, body: str = "Body.") -> None:
    content = f"---\n{frontmatter.strip()}\n---\n{body}\n"
    (roles_dir / f"{role_name}.md").write_text(content, encoding="utf-8")


def test_frontmatter_parses_tool_bundles(tmp_path: Path) -> None:
    _write_role(
        tmp_path,
        "repo_inspector",
        """
role_name: repo_inspector
tool_bundles: [fs_read, search, sprint]
allow_effects: [read, self]
allow_targets: ["read.*", "self.*"]
        """,
    )
    svc = RoleRegistryService(tmp_path)
    role = svc.get_role("repo_inspector")
    assert role.tool_bundles == ["fs_read", "search", "sprint"]
    assert role.allow_effects == ["read", "self"]
    assert role.allow_targets == ["read.*", "self.*"]


def test_frontmatter_parses_worktree_fields(tmp_path: Path) -> None:
    _write_role(
        tmp_path,
        "developer",
        """
role_name: developer
tool_bundles: [fs_read, fs_write, git_local]
concurrency_group: "repo:exampleapp"
needs_worktree: true
worktree_base_branch: main
        """,
    )
    svc = RoleRegistryService(tmp_path)
    role = svc.get_role("developer")
    assert role.concurrency_group == "repo:exampleapp"
    assert role.needs_worktree is True
    assert role.worktree_base_branch == "main"


def test_missing_worktree_fields_use_defaults(tmp_path: Path) -> None:
    _write_role(
        tmp_path,
        "researcher",
        """
role_name: researcher
tool_bundles: [fs_read, search]
        """,
    )
    svc = RoleRegistryService(tmp_path)
    role = svc.get_role("researcher")
    assert role.needs_worktree is False
    assert role.concurrency_group is None
    assert role.worktree_base_branch == "main"


def test_empty_concurrency_group_becomes_none(tmp_path: Path) -> None:
    _write_role(
        tmp_path,
        "neutral",
        """
role_name: neutral
tool_bundles: [fs_read]
concurrency_group: ""
        """,
    )
    svc = RoleRegistryService(tmp_path)
    role = svc.get_role("neutral")
    assert role.concurrency_group is None


def test_needs_worktree_quoted_false_is_false(tmp_path: Path) -> None:
    """Regression: ``needs_worktree: "false"`` (quoted str) must NOT coerce to True.

    Python's ``bool(non_empty_str)`` is always True, so the naive
    ``bool(parsed.get("needs_worktree") or False)`` pattern would
    silently enable worktree isolation for any quoted-string frontmatter
    typo. The parser uses a strict truthy/falsy keyword coercer instead.
    """
    _write_role(
        tmp_path,
        "strict_false",
        """
role_name: strict_false
tool_bundles: [fs_read]
needs_worktree: "false"
        """,
    )
    svc = RoleRegistryService(tmp_path)
    role = svc.get_role("strict_false")
    assert role.needs_worktree is False


def test_needs_worktree_accepts_common_truthy_variants(tmp_path: Path) -> None:
    """YAML 1.1 boolean variants plus quoted forms all resolve sensibly."""
    for value, expected in [
        ("true", True),
        ("'true'", True),
        ("yes", True),
        ("'yes'", True),
        ("on", True),
        ("'1'", True),
        ("false", False),
        ("'false'", False),
        ("no", False),
        ("'no'", False),
        ("off", False),
        ("'0'", False),
    ]:
        _write_role(
            tmp_path,
            f"role_{value.replace(chr(39), '')}",
            f"""
role_name: role_x
tool_bundles: [fs_read]
needs_worktree: {value}
            """,
        )

    svc = RoleRegistryService(tmp_path)
    # Iterate with structured expectations to keep the assertion explicit.
    for raw, expected in [
        ("true", True),
        ("'true'", True),
        ("yes", True),
        ("'yes'", True),
        ("on", True),
        ("'1'", True),
        ("false", False),
        ("'false'", False),
        ("no", False),
        ("'no'", False),
        ("off", False),
        ("'0'", False),
    ]:
        role = svc.get_role(f"role_{raw.replace(chr(39), '')}")
        assert role.needs_worktree is expected, (
            f"needs_worktree: {raw!r} should coerce to {expected}, "
            f"got {role.needs_worktree}"
        )


def test_needs_worktree_unknown_string_defaults_to_false(tmp_path: Path) -> None:
    """An unrecognised string fails safe: feature stays OFF."""
    _write_role(
        tmp_path,
        "typo",
        """
role_name: typo
tool_bundles: [fs_read]
needs_worktree: maybe
        """,
    )
    svc = RoleRegistryService(tmp_path)
    role = svc.get_role("typo")
    assert role.needs_worktree is False


def test_legacy_role_without_new_keys_still_parses(tmp_path: Path) -> None:
    _write_role(
        tmp_path,
        "legacy",
        """
role_name: legacy
tool_allow_list: [foo, bar]
allow_effects: [read]
        """,
    )
    svc = RoleRegistryService(tmp_path)
    role = svc.get_role("legacy")
    assert isinstance(role, RoleDefinition)
    assert role.tool_bundles == []
    assert role.concurrency_group is None
    assert role.needs_worktree is False
    assert role.worktree_base_branch == "main"
    assert role.tool_allow_list == ["foo", "bar"]
