from __future__ import annotations

from pathlib import Path

import pytest

from feishu_agent.roles.role_registry_service import (
    RoleNotFoundError,
    RoleRegistryService,
)

FULL_ROLE = """\
---
tags: [plan, execute]
tool_allow_list: [read_sprint_status, advance_sprint_state]
model: doubao-seed-2-0-pro
---

# Sprint Planner

You convert approved direction into staged delivery goals.
"""

MINIMAL_ROLE = """\
---
tags: [review]
---

# Reviewer

You review code and documentation.
"""

NO_FRONTMATTER = """\
# Legacy Role

No YAML frontmatter here.
"""


@pytest.fixture()
def roles_dir(tmp_path: Path) -> Path:
    d = tmp_path / "roles"
    d.mkdir()
    (d / "sprint_planner.md").write_text(FULL_ROLE, encoding="utf-8")
    (d / "reviewer.md").write_text(MINIMAL_ROLE, encoding="utf-8")
    (d / "legacy.md").write_text(NO_FRONTMATTER, encoding="utf-8")
    return d


def test_get_role_full_frontmatter(roles_dir: Path):
    svc = RoleRegistryService(roles_dir)
    role = svc.get_role("sprint_planner")

    assert role.role_name == "sprint_planner"
    assert role.tags == ["plan", "execute"]
    assert role.tool_allow_list == ["read_sprint_status", "advance_sprint_state"]
    assert role.model == "doubao-seed-2-0-pro"
    assert "Sprint Planner" in role.system_prompt


def test_get_role_minimal_frontmatter(roles_dir: Path):
    svc = RoleRegistryService(roles_dir)
    role = svc.get_role("reviewer")

    assert role.tags == ["review"]
    assert role.tool_allow_list == []
    assert role.model is None
    assert "Reviewer" in role.system_prompt


def test_get_role_no_frontmatter(roles_dir: Path):
    svc = RoleRegistryService(roles_dir)
    role = svc.get_role("legacy")

    assert role.tags == []
    assert role.tool_allow_list == []
    assert "Legacy Role" in role.system_prompt


def test_get_role_not_found(roles_dir: Path):
    svc = RoleRegistryService(roles_dir)

    with pytest.raises(RoleNotFoundError):
        svc.get_role("nonexistent")


def test_list_roles_all(roles_dir: Path):
    svc = RoleRegistryService(roles_dir)
    roles = svc.list_roles()

    assert len(roles) == 3
    names = {r.role_name for r in roles}
    assert names == {"sprint_planner", "reviewer", "legacy"}


def test_list_roles_with_tag_filter(roles_dir: Path):
    svc = RoleRegistryService(roles_dir)

    plan_roles = svc.list_roles(tag="plan")
    assert len(plan_roles) == 1
    assert plan_roles[0].role_name == "sprint_planner"

    review_roles = svc.list_roles(tag="review")
    assert len(review_roles) == 1
    assert review_roles[0].role_name == "reviewer"

    assert svc.list_roles(tag="nonexistent") == []


def test_register_and_get_executor_factory(roles_dir: Path):
    svc = RoleRegistryService(roles_dir)

    def my_factory():
        return "executor"

    svc.register_executor_factory("sprint_planner", my_factory)

    assert svc.get_executor_factory("sprint_planner") is my_factory
    assert svc.get_executor_factory("unknown") is None


def test_malformed_yaml_frontmatter_does_not_crash(roles_dir: Path):
    (roles_dir / "broken.md").write_text(
        "---\ntags: [unclosed\n---\n\n# Broken Role\n",
        encoding="utf-8",
    )
    svc = RoleRegistryService(roles_dir)

    roles = svc.list_roles()
    broken = [r for r in roles if r.role_name == "broken"]
    assert len(broken) == 1
    assert broken[0].tags == []
    assert "Broken Role" in broken[0].system_prompt or "tags:" in broken[0].system_prompt


def test_allow_effects_and_allow_targets_roundtrip(roles_dir: Path):
    (roles_dir / "m3_role.md").write_text(
        """\
---
tags: [review]
allow_effects: [self, read]
allow_targets: [\"self.*\", \"world.git.read_*\"]
---

# M3 Role

Declarative policy example.
""",
        encoding="utf-8",
    )

    svc = RoleRegistryService(roles_dir)
    role = svc.get_role("m3_role")
    assert role.allow_effects == ["self", "read"]
    assert role.allow_targets == ["self.*", "world.git.read_*"]
    # tool_allow_list stays empty unless explicitly set
    assert role.tool_allow_list == []


def test_allow_effects_defaults_to_empty_when_missing(roles_dir: Path):
    svc = RoleRegistryService(roles_dir)
    # The pre-existing reviewer.md has no allow_effects / allow_targets.
    role = svc.get_role("reviewer")
    assert role.allow_effects == []
    assert role.allow_targets == []


def test_hot_reload_on_file_change(roles_dir: Path):
    svc = RoleRegistryService(roles_dir)

    role_v1 = svc.get_role("sprint_planner")
    assert role_v1.model == "doubao-seed-2-0-pro"

    updated = FULL_ROLE.replace("doubao-seed-2-0-pro", "gpt-4o")
    (roles_dir / "sprint_planner.md").write_text(updated, encoding="utf-8")

    role_v2 = svc.get_role("sprint_planner")
    assert role_v2.model == "gpt-4o"
