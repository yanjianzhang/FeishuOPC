"""Integration tests for bundle-driven role dispatch (A-2 Wave 3).

These exercise the happy path that ``_build_role_executor_provider``
takes when a role's frontmatter declares ``tool_bundles: [...]``: the
provider builds a :class:`BundleContext` from the same service dict
it would pass to a legacy factory, then constructs a
:class:`GenericRoleExecutor` instead of instantiating a hand-written
executor class.

The tests stop short of invoking the LLM — they assert the
*composition* of the executor (right tool names, right filters
applied) and that one tool can actually dispatch through the
composite handler path. End-to-end LLM-driven dispatch is covered by
the harness-level tests in ``test_tech_lead_executor_harness.py``.

Why two roles and not all six? repo_inspector exercises the
``[sprint, bitable_read, fs_write]`` combo (three bundles, two
services), while sprint_planner exercises ``[sprint, search]``
(two bundles, one of which — ``search`` — depends on a
``WorkflowService`` the other role doesn't touch). Together they
cover every bundle shape the 6 migrated roles rely on.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from feishu_agent.roles.generic_role_executor import GenericRoleExecutor
from feishu_agent.roles.role_executors import register_role_executors
from feishu_agent.roles.role_registry_service import RoleRegistryService
from feishu_agent.runtime.feishu_runtime_service import (
    FeishuBotContext,
    _build_role_executor_provider,
)
from feishu_agent.team.role_artifact_writer import RoleArtifactWriter
from feishu_agent.team.sprint_state_service import SprintStateService
from feishu_agent.tools.progress_sync_service import ProgressSyncService
from feishu_agent.tools.workflow_service import WorkflowService

ROLES_DIR = Path(__file__).resolve().parents[2] / "skills" / "roles"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _role_registry() -> RoleRegistryService:
    """Load the real skills/roles frontmatter + register legacy factories.

    Uses the checked-in markdown (not a fixture tree) so the test
    catches drift between bundle assignments declared in the spec and
    what the provider actually exposes.
    """
    registry = RoleRegistryService(ROLES_DIR)
    register_role_executors(registry)
    return registry


def _seed_status(tmp_path: Path) -> Path:
    """Minimal sprint-status.yaml the sprint bundle can load."""
    status = tmp_path / "sprint-status.yaml"
    status.write_text(
        yaml.safe_dump(
            {
                "sprint_name": "sprint-1",
                "current_sprint": {"goal": "ship A-2"},
                "development_status": {},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return status


def _make_provider_with_overrides(tmp_path: Path, **kwargs):
    """Construct the real provider with stand-in services injected by
    patching ``_wire_shared``'s outputs via a spy factory.

    The provider itself is unchanged; we just supply a
    ``sprint_state`` backed by a real YAML file and a
    ``progress_service`` whose ``repo_root`` points at ``tmp_path``
    so bundle handlers find predictable state.
    """
    _seed_status(tmp_path)

    sprint = SprintStateService(tmp_path, "sprint-status.yaml")
    progress = MagicMock(spec=ProgressSyncService)
    progress.repo_root = tmp_path

    ctx_obj = FeishuBotContext(
        bot_name="test",
        app_id="x",
        app_secret="y",
        verification_token=None,
        encrypt_key=None,
    )
    return _build_role_executor_provider(
        role_registry=_role_registry(),
        progress_service=progress,
        sprint_state=sprint,
        project_id="test-project",
        command_text="测试指令",
        repo_root=tmp_path,
        context=ctx_obj,
        registry=None,
        trace_id="trace-test",
        chat_id="chat-test",
    )


# ---------------------------------------------------------------------------
# T037 — TL → repo_inspector via GenericRoleExecutor
# ---------------------------------------------------------------------------


def test_repo_inspector_routed_through_generic_executor(tmp_path: Path) -> None:
    provider = _make_provider_with_overrides(tmp_path)
    role = _role_registry().get_role("repo_inspector")

    executor = provider("repo_inspector", role)

    assert isinstance(executor, GenericRoleExecutor), (
        "repo_inspector must route through GenericRoleExecutor once "
        "its frontmatter declares tool_bundles."
    )
    # frontmatter: tool_bundles: [sprint, bitable_read, fs_write]
    # bitable_read depends on progress_sync (present) — yields 3 tools
    # sprint depends on sprint_service (present) — yields 2 tools
    # fs_write depends on role_artifact_writer (NOT present here; no
    # ProjectRegistry) — yields 0 tools. tool_allow_list filters the
    # surface to just names the role declared.
    names = {s.name for s in executor.tool_specs()}
    # allow_list is {read_sprint_status, read_bitable_rows,
    # write_role_artifact}; only the first two are provided by the
    # wired bundles so those are the ones the final surface exposes.
    assert names == {"read_sprint_status", "read_bitable_rows"}


@pytest.mark.asyncio
async def test_repo_inspector_dispatches_sprint_tool(tmp_path: Path) -> None:
    provider = _make_provider_with_overrides(tmp_path)
    role = _role_registry().get_role("repo_inspector")

    executor = provider("repo_inspector", role)

    result = await executor.execute_tool("read_sprint_status", {})
    assert result["sprint_name"] == "sprint-1"
    assert result["goal"] == "ship A-2"


def test_repo_inspector_gets_artifact_tool_when_writer_wired(
    tmp_path: Path,
) -> None:
    """With a RoleArtifactWriter wired, fs_write contributes
    ``write_role_artifact`` and the composite surface matches the
    role's declared allow_list."""

    writer = RoleArtifactWriter(
        role_name="repo_inspector",
        project_id="test-project",
        allowed_write_root=tmp_path / "docs" / "repo-analysis",
    )
    # Build the generic executor manually so we can inject the writer
    # without fabricating a ProjectRegistry.
    from feishu_agent.runtime.feishu_runtime_service import _BUNDLE_REGISTRY
    from feishu_agent.tools.bundle_context import BundleContext

    sprint = SprintStateService(tmp_path, "sprint-status.yaml")
    _seed_status(tmp_path)

    ctx = BundleContext(
        working_dir=tmp_path,
        repo_root=tmp_path,
        chat_id="c",
        trace_id="t",
        role_name="repo_inspector",
        project_id="test-project",
        sprint_service=sprint,
        progress_sync_service=MagicMock(spec=ProgressSyncService),
        role_artifact_writer=writer,
    )
    role = _role_registry().get_role("repo_inspector")
    executor = GenericRoleExecutor(role, _BUNDLE_REGISTRY, ctx)
    names = {s.name for s in executor.tool_specs()}
    assert "write_role_artifact" in names


# ---------------------------------------------------------------------------
# T038 — TL → sprint_planner via bundle routing
# ---------------------------------------------------------------------------


def test_sprint_planner_routed_through_generic_executor(
    tmp_path: Path,
) -> None:
    provider = _make_provider_with_overrides(tmp_path)
    role = _role_registry().get_role("sprint_planner")

    executor = provider("sprint_planner", role)

    assert isinstance(executor, GenericRoleExecutor), (
        "sprint_planner must route through GenericRoleExecutor once "
        "its frontmatter declares tool_bundles."
    )
    # frontmatter: tool_bundles: [sprint, search]
    # search needs workflow_service (NOT wired without a
    # ProjectRegistry, same reason as above) — empty set.
    # sprint yields 2 tools. tool_allow_list filters; the two sprint
    # tools survive, the workflow-read tools do not.
    names = {s.name for s in executor.tool_specs()}
    assert names == {"read_sprint_status", "advance_sprint_state"}


@pytest.mark.asyncio
async def test_sprint_planner_dispatches_read_sprint_status(
    tmp_path: Path,
) -> None:
    provider = _make_provider_with_overrides(tmp_path)
    role = _role_registry().get_role("sprint_planner")

    executor = provider("sprint_planner", role)

    # read_sprint_status routes through the sprint bundle's handler
    # → SprintStateService.load_status_data → YAML we seeded.
    # This is the primary verification that bundle composition ends
    # up with a callable, service-backed tool rather than a spec
    # without a handler.
    result = await executor.execute_tool("read_sprint_status", {})
    assert result["sprint_name"] == "sprint-1"
    assert result["goal"] == "ship A-2"


def test_sprint_planner_gets_workflow_tools_when_service_wired(
    tmp_path: Path,
) -> None:
    """With a WorkflowService wired, search bundle contributes the
    three workflow-read tools and the composite surface matches the
    role's full declared allow_list."""
    from feishu_agent.runtime.feishu_runtime_service import _BUNDLE_REGISTRY
    from feishu_agent.tools.bundle_context import BundleContext

    workflow = MagicMock(spec=WorkflowService)
    sprint = SprintStateService(tmp_path, "sprint-status.yaml")
    _seed_status(tmp_path)

    ctx = BundleContext(
        working_dir=tmp_path,
        repo_root=tmp_path,
        chat_id="c",
        trace_id="t",
        role_name="sprint_planner",
        project_id="test-project",
        sprint_service=sprint,
        workflow_service=workflow,
    )
    role = _role_registry().get_role("sprint_planner")
    executor = GenericRoleExecutor(role, _BUNDLE_REGISTRY, ctx)
    names = {s.name for s in executor.tool_specs()}
    assert names == {
        "read_sprint_status",
        "advance_sprint_state",
        "read_workflow_instruction",
        "list_workflow_artifacts",
        "read_repo_file",
    }


# ---------------------------------------------------------------------------
# Review-fix M1 — every migrated role reaches GenericRoleExecutor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role_name",
    [
        "repo_inspector",
        "sprint_planner",
        "researcher",
        "spec_linker",
        "ux_designer",
        "qa_tester",
    ],
)
def test_all_migrated_roles_route_through_generic_executor(
    tmp_path: Path, role_name: str
) -> None:
    """Guard against bundle-list typos in any of the 6 migrated roles.

    The detailed per-role tests above only cover ``repo_inspector`` and
    ``sprint_planner``. A typo like ``[sprnit, bitable_read]`` in the
    other four would pass frontmatter parsing (lists of strings are
    always valid YAML) and silently turn the role into a no-tool
    sub-agent at dispatch time. This parametric test closes the gap by
    building the provider for every migrated role and asserting:

    1. The provider returns a ``GenericRoleExecutor`` (not ``None``,
       not a legacy instance) — proves the bundle branch fires.
    2. ``tool_specs()`` is non-empty after ``tool_allow_list`` filtering
       — proves at least one bundle in the declared list actually
       resolved to tools given the minimal service wiring.

    Sprint service is the common denominator we DO wire here
    (``SprintStateService`` backed by a real YAML file); bundles that
    need ``workflow_service`` / ``role_artifact_writer`` / etc. will
    contribute nothing in this fixture, but every role's allow_list
    includes at least one sprint or bitable tool, so the surface stays
    non-empty.
    """
    provider = _make_provider_with_overrides(tmp_path)
    role = _role_registry().get_role(role_name)
    assert role is not None, f"{role_name} must exist in skills/roles/"

    executor = provider(role_name, role)

    assert isinstance(executor, GenericRoleExecutor), (
        f"{role_name} must route through GenericRoleExecutor — its "
        "frontmatter declares tool_bundles but the provider returned "
        f"{type(executor).__name__}"
    )
    names = {s.name for s in executor.tool_specs()}
    assert names, (
        f"{role_name} has an empty tool surface after bundle composition "
        "— likely a tool_bundles typo or a missing service wiring"
    )


def test_migrated_roles_survive_missing_repo_root(tmp_path: Path) -> None:
    """Review-fix H1 regression guard: with ``repo_root=None`` (the
    ``settings.app_repo_root`` unset state) the provider MUST still
    return a GenericRoleExecutor for bundle-driven roles. Before the
    fix, the ``repo_root is not None`` guard silently dropped the
    entire surface because the deleted legacy factory no longer
    existed."""
    _seed_status(tmp_path)
    sprint = SprintStateService(tmp_path, "sprint-status.yaml")
    progress = MagicMock(spec=ProgressSyncService)
    progress.repo_root = tmp_path

    ctx_obj = FeishuBotContext(
        bot_name="test",
        app_id="x",
        app_secret="y",
        verification_token=None,
        encrypt_key=None,
    )
    provider = _build_role_executor_provider(
        role_registry=_role_registry(),
        progress_service=progress,
        sprint_state=sprint,
        project_id="test-project",
        command_text="测试指令",
        repo_root=None,  # the regression trigger
        context=ctx_obj,
        registry=None,
        trace_id="trace-test",
        chat_id="chat-test",
    )

    role = _role_registry().get_role("sprint_planner")
    executor = provider("sprint_planner", role)
    assert isinstance(executor, GenericRoleExecutor), (
        "Bundle-driven roles must remain functional even when "
        "repo_root is unresolved — see H1 in Wave 3 code review."
    )
    # sprint service is wired, so at least sprint-bundle tools should
    # still appear after allow_list filtering.
    names = {s.name for s in executor.tool_specs()}
    assert "read_sprint_status" in names


# ---------------------------------------------------------------------------
# Fallback — prd_writer keeps its legacy executor
# ---------------------------------------------------------------------------


def test_prd_writer_falls_back_to_legacy_executor(tmp_path: Path) -> None:
    """prd_writer's frontmatter does NOT declare tool_bundles (its
    ``write_file`` tool is intentionally outside the bundle surface —
    see A-2 Wave 2 review, M1). The provider must therefore hand the
    dispatch over to ``PrdWriterExecutor``, not GenericRoleExecutor.

    This is the regression guard for the "fold write_file into
    fs_write" mistake — if somebody decides to migrate prd_writer
    without updating the wiring, this test fails first.
    """
    provider = _make_provider_with_overrides(tmp_path)
    role = _role_registry().get_role("prd_writer")

    executor = provider("prd_writer", role)

    # With repo_root=tmp_path and no ProjectRegistry, prd_writer's
    # wiring mutator actually succeeds (``prd_write_root = tmp_path /
    # "specs"``) and returns a legacy PrdWriterExecutor.
    assert executor is not None
    assert not isinstance(executor, GenericRoleExecutor)
    assert type(executor).__name__ == "PrdWriterExecutor"
