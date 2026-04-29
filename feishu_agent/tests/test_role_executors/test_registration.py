from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
import yaml
from feishu_agent.core.llm_gateway_shim import MockGateway as _BaseMockGateway

from feishu_agent.core.llm_agent_adapter import LlmAgentAdapter
from feishu_agent.roles.role_executors import (
    BugFixerExecutor,
    DeployEngineerExecutor,
    DeveloperExecutor,
    PrdWriterExecutor,
    ProgressSyncExecutor,
    ReviewerExecutor,
    register_role_executors,
)
from feishu_agent.roles.role_registry_service import RoleRegistryService
from feishu_agent.roles.tech_lead_executor import TechLeadToolExecutor
from feishu_agent.team.audit_service import AuditService
from feishu_agent.team.sprint_state_service import SprintStateService
from feishu_agent.tools.progress_sync_service import ProgressSyncService

ROLES_DIR = Path(__file__).resolve().parents[3] / "skills" / "roles"

# Roles that still ship a hand-written executor class. The other six
# (sprint_planner / repo_inspector / qa_tester / researcher /
# spec_linker / ux_designer) declare ``tool_bundles`` in their
# frontmatter and are served by :class:`GenericRoleExecutor` at
# dispatch time (see ``test_generic_role_executor_integration.py``),
# so they have no factory entry and never appear in this sweep.
LEGACY_REGISTERED_ROLES = [
    "progress_sync",
    "reviewer",
    "prd_writer",
    "developer",
    "bug_fixer",
    "deploy_engineer",
]

BUNDLE_DRIVEN_ROLES = [
    "sprint_planner",
    "repo_inspector",
    "qa_tester",
    "researcher",
    "spec_linker",
    "ux_designer",
]


# ======================================================================
# T073 — register_role_executors (all registered roles)
# ======================================================================


def test_register_role_executors_populates_registry():
    registry = RoleRegistryService(ROLES_DIR)
    register_role_executors(registry)
    assert registry.get_executor_factory("progress_sync") is ProgressSyncExecutor
    assert registry.get_executor_factory("reviewer") is ReviewerExecutor
    assert registry.get_executor_factory("prd_writer") is PrdWriterExecutor
    assert registry.get_executor_factory("developer") is DeveloperExecutor
    assert registry.get_executor_factory("bug_fixer") is BugFixerExecutor
    assert registry.get_executor_factory("deploy_engineer") is DeployEngineerExecutor


def test_bundle_driven_roles_have_no_factory():
    """Post-Wave-3 guard: the six migrated roles MUST NOT have a
    legacy factory registered. They are served by the bundle-driven
    ``GenericRoleExecutor`` via their frontmatter ``tool_bundles``
    declaration — adding them back to ``LEGACY_ROLE_FACTORIES`` would
    cause the provider to hand out a stale executor class alongside
    the generic one."""
    registry = RoleRegistryService(ROLES_DIR)
    register_role_executors(registry)
    for role_name in BUNDLE_DRIVEN_ROLES:
        assert registry.get_executor_factory(role_name) is None, (
            f"{role_name} must NOT have a legacy factory after A-2 Wave 3"
        )


def test_full_registry_sweep_legacy_roles():
    """Keep the registry coverage list in lock-step with legacy roles."""
    registry = RoleRegistryService(ROLES_DIR)
    register_role_executors(registry)
    for role_name in LEGACY_REGISTERED_ROLES:
        factory = registry.get_executor_factory(role_name)
        assert factory is not None, f"Factory missing for role: {role_name}"
    assert len(LEGACY_REGISTERED_ROLES) == 6


# ======================================================================
# Integration tests — dispatch_role_agent via llm_gateway_shim MockGateway (tests only)
# ======================================================================
#
# NOTE (A-2 Wave 3 / review M2): the six ``test_dispatch_*_integration``
# tests for bundle-driven roles (sprint_planner / repo_inspector /
# qa_tester / researcher / spec_linker / ux_designer) now exercise the
# ``factory is None -> spawn_sub_agent`` fallback rather than the real
# dispatch chain, because those factories no longer exist.
# ``HttpOnlyMockGateway.chat.send`` returns a canned success payload
# regardless of how the TL routes, so the asserts below pass even if
# the provider hands back ``None`` or a stub.
#
# The provider-level bundle routing for these roles is properly
# verified in ``test_generic_role_executor_integration.py``
# (parametric test across all 6). The tests here are kept as smoke
# coverage for the TL's MockGateway fallback path — they prove that
# the end-to-end call surface does not crash when a role has no
# registered factory and no provider, which is a real production
# state (``_runtime_repo_root()`` returning ``None`` before the H1 fix
# landed).


class HttpOnlyMockGateway(_BaseMockGateway):
    async def subscribe(self, event_types=None):
        raise NotImplementedError("Forces HTTP-only execute path")


def _build_chat_response(content: str = "ok") -> dict:
    return {
        "runId": str(uuid.uuid4()),
        "content": content,
        "status": "completed",
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


def _write_role_file(roles_dir: Path, name: str, *, tags: list[str], tool_allow_list: list[str], body: str) -> None:
    fm = yaml.safe_dump({"tags": tags, "tool_allow_list": tool_allow_list}, default_flow_style=True).strip()
    (roles_dir / f"{name}.md").write_text(f"---\n{fm}\n---\n{body}", encoding="utf-8")


@pytest_asyncio.fixture()
async def integration_setup(tmp_path: Path):
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()
    _write_role_file(
        roles_dir, "sprint_planner",
        tags=["plan"], tool_allow_list=["read_sprint_status", "advance_sprint_state"],
        body="You are the Sprint Planner.",
    )
    _write_role_file(
        roles_dir, "repo_inspector",
        tags=["execute"], tool_allow_list=["read_sprint_status", "read_bitable_rows"],
        body="You are the Repo Inspector.",
    )
    _write_role_file(
        roles_dir, "progress_sync",
        tags=["execute", "review"], tool_allow_list=["preview_progress_sync", "write_progress_sync", "resolve_bitable_target"],
        body="You are the Progress Sync agent.",
    )
    _write_role_file(
        roles_dir, "reviewer",
        tags=["plan", "review"], tool_allow_list=["read_sprint_status", "read_bitable_rows", "read_bitable_schema"],
        body="You are the Reviewer.",
    )
    _write_role_file(
        roles_dir, "qa_tester",
        tags=["execute", "review"], tool_allow_list=["read_sprint_status", "read_bitable_rows"],
        body="You are the QA Tester.",
    )
    _write_role_file(
        roles_dir, "prd_writer",
        tags=["plan"], tool_allow_list=["write_file"],
        body="You are the PRD Writer.",
    )
    _write_role_file(
        roles_dir, "researcher",
        tags=["brainstorm", "plan"], tool_allow_list=["read_bitable_rows", "read_bitable_schema"],
        body="You are the Researcher.",
    )
    _write_role_file(
        roles_dir, "spec_linker",
        tags=["brainstorm", "plan", "review"], tool_allow_list=["read_sprint_status", "read_bitable_rows"],
        body="You are the Spec Linker.",
    )
    _write_role_file(
        roles_dir, "ux_designer",
        tags=["brainstorm", "plan", "review"], tool_allow_list=["read_bitable_rows"],
        body="You are the UX Designer.",
    )

    mock_gw = HttpOnlyMockGateway()
    mock_gw.register("agents.create", lambda p: {"agentId": p.get("agentId", "test"), "status": "created"})
    mock_gw.register("chat.send", lambda p: _build_chat_response("Planned sprint 5 successfully"))
    mock_gw.register("config.get", lambda p: {"agentId": "test", "tools": {}})
    mock_gw.register("config.set", lambda p: {"ok": True})
    mock_gw.register("config.patch", lambda p: {"ok": True})
    await mock_gw.connect()

    adapter = LlmAgentAdapter(
        gateway_url="ws://mock:18789/gateway",
        default_model="doubao-seed-2-0-pro-260215",
        gateway=mock_gw,
        timeout=30,
    )
    await adapter.connect()

    status_file = "sprint-status.yaml"
    (tmp_path / status_file).write_text(
        yaml.safe_dump({"sprint_name": "Sprint 5", "current_sprint": {"goal": "Integration"}}, allow_unicode=True),
        encoding="utf-8",
    )

    registry = RoleRegistryService(roles_dir)
    register_role_executors(registry)

    executor = TechLeadToolExecutor(
        progress_sync_service=MagicMock(spec=ProgressSyncService),
        sprint_state_service=SprintStateService(tmp_path, status_file),
        audit_service=AuditService(tmp_path / "audit"),
        llm_agent_adapter=adapter,
        role_registry=registry,
        project_id="test-project",
        command_text="integration test",
        trace_id="trace-integration",
        # 600s matches production to clear ``MIN_SUB_AGENT_TIMEOUT_SECONDS``;
        # a lower value would trip the new budget-refusal gate in
        # TechLeadToolExecutor._dispatch_role_agent.
        timeout_seconds=600,
    )
    return executor


@pytest.mark.asyncio
async def test_dispatch_sprint_planner_integration(integration_setup):
    """Full chain: TechLeadToolExecutor -> dispatch_role_agent -> SDK mock gateway for sprint_planner."""
    executor = integration_setup
    result = await executor.execute_tool("dispatch_role_agent", {
        "role_name": "sprint_planner",
        "task": "Plan the sprint 5 iteration",
    })
    assert result["success"] is True
    assert result["role_name"] == "sprint_planner"
    assert isinstance(result["output"], str)
    assert result["error"] is None
    assert isinstance(result["latency_ms"], int)


@pytest.mark.asyncio
async def test_dispatch_repo_inspector_integration(integration_setup):
    """Full chain: TechLeadToolExecutor -> dispatch_role_agent -> SDK mock gateway for repo_inspector."""
    executor = integration_setup
    result = await executor.execute_tool("dispatch_role_agent", {
        "role_name": "repo_inspector",
        "task": "Inspect the repository structure",
    })
    assert result["success"] is True
    assert result["role_name"] == "repo_inspector"
    assert isinstance(result["output"], str)
    assert result["error"] is None


@pytest.mark.asyncio
async def test_dispatch_progress_sync_integration(integration_setup):
    """Full chain: TechLeadToolExecutor -> dispatch_role_agent -> SDK mock gateway for progress_sync."""
    executor = integration_setup
    result = await executor.execute_tool("dispatch_role_agent", {
        "role_name": "progress_sync",
        "task": "Preview progress sync for current sprint",
    })
    assert result["success"] is True
    assert result["role_name"] == "progress_sync"
    assert isinstance(result["output"], str)
    assert result["error"] is None
    assert isinstance(result["latency_ms"], int)


@pytest.mark.asyncio
async def test_dispatch_reviewer_integration(integration_setup):
    """Full chain: TechLeadToolExecutor -> dispatch_role_agent -> SDK mock gateway for reviewer."""
    executor = integration_setup
    result = await executor.execute_tool("dispatch_role_agent", {
        "role_name": "reviewer",
        "task": "Review the code changes in Sprint 5",
    })
    assert result["success"] is True
    assert result["role_name"] == "reviewer"
    assert isinstance(result["output"], str)
    assert result["error"] is None
    assert isinstance(result["latency_ms"], int)


@pytest.mark.asyncio
async def test_dispatch_qa_tester_integration(integration_setup):
    """Full chain: TechLeadToolExecutor -> dispatch_role_agent -> SDK mock gateway for qa_tester."""
    executor = integration_setup
    result = await executor.execute_tool("dispatch_role_agent", {
        "role_name": "qa_tester",
        "task": "Run QA validation on the sprint deliverables",
    })
    assert result["success"] is True
    assert result["role_name"] == "qa_tester"
    assert isinstance(result["output"], str)
    assert result["error"] is None
    assert isinstance(result["latency_ms"], int)


@pytest.mark.asyncio
async def test_dispatch_prd_writer_integration(integration_setup):
    """Full chain: TechLeadToolExecutor -> dispatch_role_agent -> SDK mock gateway for prd_writer."""
    executor = integration_setup
    result = await executor.execute_tool("dispatch_role_agent", {
        "role_name": "prd_writer",
        "task": "Write a PRD for the OCR scan feature",
    })
    assert result["success"] is True
    assert result["role_name"] == "prd_writer"
    assert isinstance(result["output"], str)
    assert result["error"] is None
    assert isinstance(result["latency_ms"], int)


@pytest.mark.asyncio
async def test_dispatch_researcher_integration(integration_setup):
    """Full chain: TechLeadToolExecutor -> dispatch_role_agent -> SDK mock gateway for researcher."""
    executor = integration_setup
    result = await executor.execute_tool("dispatch_role_agent", {
        "role_name": "researcher",
        "task": "Research prior auth design decisions from the spec archive",
    })
    assert result["success"] is True
    assert result["role_name"] == "researcher"
    assert isinstance(result["output"], str)
    assert result["error"] is None
    assert isinstance(result["latency_ms"], int)


@pytest.mark.asyncio
async def test_dispatch_spec_linker_integration(integration_setup):
    """Full chain: TechLeadToolExecutor -> dispatch_role_agent -> SDK mock gateway for spec_linker."""
    executor = integration_setup
    result = await executor.execute_tool("dispatch_role_agent", {
        "role_name": "spec_linker",
        "task": "Link the notification feature request to existing specs",
    })
    assert result["success"] is True
    assert result["role_name"] == "spec_linker"
    assert isinstance(result["output"], str)
    assert result["error"] is None
    assert isinstance(result["latency_ms"], int)


@pytest.mark.asyncio
async def test_dispatch_ux_designer_integration(integration_setup):
    """Full chain: TechLeadToolExecutor -> dispatch_role_agent -> SDK mock gateway for ux_designer."""
    executor = integration_setup
    result = await executor.execute_tool("dispatch_role_agent", {
        "role_name": "ux_designer",
        "task": "Evaluate the onboarding flow for friction points",
    })
    assert result["success"] is True
    assert result["role_name"] == "ux_designer"
    assert isinstance(result["output"], str)
    assert result["error"] is None
    assert isinstance(result["latency_ms"], int)
