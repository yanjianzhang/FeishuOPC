from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from feishu_agent.roles.role_executors.reviewer import (
    REVIEWER_CODE_READ_ALLOW,
    REVIEWER_TOOL_SPECS,
    ReviewerExecutor,
)
from feishu_agent.roles.role_registry_service import RoleRegistryService
from feishu_agent.team.role_artifact_writer import RoleArtifactWriter
from feishu_agent.team.sprint_state_service import SprintStateService
from feishu_agent.tools.code_write_service import CodeWriteService
from feishu_agent.tools.progress_sync_service import ProgressSyncService

ROLES_DIR = Path(__file__).resolve().parents[3] / "skills" / "roles"


def _make_executor(
    tmp_path: Path,
    *,
    status_data: dict[str, Any] | None = None,
    bitable_tables: dict[str, Any] | None = None,
    role_permissions: list[Any] | None = None,
) -> ReviewerExecutor:
    status_file = "sprint-status.yaml"
    status_path = tmp_path / status_file
    data = status_data or {"sprint_name": "Sprint 5", "current_sprint": {"goal": "Review"}}
    status_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    mock_sync = MagicMock(spec=ProgressSyncService)
    mock_sync.repo_root = tmp_path

    return ReviewerExecutor(
        sprint_state_service=SprintStateService(tmp_path, status_file),
        progress_sync_service=mock_sync,
        project_id="test-project",
        command_text="test command",
        load_bitable_tables=lambda: bitable_tables or {},
        load_role_permissions=lambda _: role_permissions or [],
    )


# ======================================================================
# tool_specs()
# ======================================================================


def test_tool_specs_returns_exactly_3_tools(tmp_path: Path):
    executor = _make_executor(tmp_path)
    specs = executor.tool_specs()
    assert len(specs) == 3


def test_tool_specs_names(tmp_path: Path):
    executor = _make_executor(tmp_path)
    names = {s.name for s in executor.tool_specs()}
    assert names == {"read_sprint_status", "read_bitable_rows", "read_bitable_schema"}


def test_tool_specs_returns_fresh_list(tmp_path: Path):
    executor = _make_executor(tmp_path)
    a = executor.tool_specs()
    b = executor.tool_specs()
    assert a is not b
    assert a == b


def test_module_level_specs_match_instance(tmp_path: Path):
    executor = _make_executor(tmp_path)
    assert [s.name for s in executor.tool_specs()] == [s.name for s in REVIEWER_TOOL_SPECS]


# ======================================================================
# execute_tool handlers
# ======================================================================


@pytest.mark.asyncio
async def test_read_sprint_status(tmp_path: Path):
    status_data = {
        "sprint_name": "Sprint 6",
        "current_sprint": {
            "goal": "Review cycle",
            "review": ["6-1-code-review"],
        },
    }
    executor = _make_executor(tmp_path, status_data=status_data)
    result = await executor.execute_tool("read_sprint_status", {})
    assert result["sprint_name"] == "Sprint 6"
    assert result["goal"] == "Review cycle"


@pytest.mark.asyncio
async def test_read_bitable_rows_no_candidates_raises(tmp_path: Path):
    executor = _make_executor(tmp_path)
    with pytest.raises(RuntimeError, match="No readable Feishu Bitable target"):
        await executor.execute_tool("read_bitable_rows", {})


@pytest.mark.asyncio
async def test_read_bitable_rows_happy_path(tmp_path: Path):
    perm = MagicMock()
    perm.table_name = "review_table"
    perm.can_read = True
    perm.can_write = False
    perm.is_default = True
    table_obj = MagicMock()

    mock_service = MagicMock(spec=ProgressSyncService)
    mock_service.read_bitable_rows = AsyncMock(return_value={
        "rows": [{"record_id": "r1", "fields": {"status": "review"}}],
        "total": 1,
        "has_more": False,
    })

    executor = _make_executor(
        tmp_path,
        bitable_tables={"review_table": table_obj},
        role_permissions=[perm],
    )
    executor._build_progress_sync_service_for_target = lambda _tn, _bt: mock_service

    result = await executor.execute_tool("read_bitable_rows", {"limit": 10})
    assert result["total"] == 1
    assert result["target_table_name"] == "review_table"


@pytest.mark.asyncio
async def test_read_bitable_schema_no_candidates_raises(tmp_path: Path):
    executor = _make_executor(tmp_path)
    with pytest.raises(RuntimeError, match="No readable Feishu Bitable target"):
        await executor.execute_tool("read_bitable_schema", {})


@pytest.mark.asyncio
async def test_read_bitable_schema_happy_path(tmp_path: Path):
    perm = MagicMock()
    perm.table_name = "review_table"
    perm.can_read = True
    perm.can_write = False
    perm.is_default = True
    table_obj = MagicMock()

    mock_service = MagicMock(spec=ProgressSyncService)
    mock_service.read_bitable_schema = AsyncMock(return_value={
        "table_name": "review_table",
        "fields": [{"name": "Status", "type": "text"}],
    })

    executor = _make_executor(
        tmp_path,
        bitable_tables={"review_table": table_obj},
        role_permissions=[perm],
    )
    executor._build_progress_sync_service_for_target = lambda _tn, _bt: mock_service

    result = await executor.execute_tool("read_bitable_schema", {})
    assert result["target_table_name"] == "review_table"
    assert len(result["fields"]) == 1


@pytest.mark.asyncio
async def test_unsupported_tool_raises(tmp_path: Path):
    executor = _make_executor(tmp_path)
    with pytest.raises(RuntimeError, match="Unsupported tool"):
        await executor.execute_tool("nonexistent", {})


# ======================================================================
# Code-read surface (bmad:code-review)
# ======================================================================


def _make_reviewer_with_code_service(
    tmp_path: Path,
    *,
    role_artifact_writer: RoleArtifactWriter | None = None,
) -> tuple[ReviewerExecutor, MagicMock]:
    """Build a reviewer with a mock ``CodeWriteService`` wired in.

    Returns both so tests can stub specific service methods.
    """
    status_file = "sprint-status.yaml"
    (tmp_path / status_file).write_text(
        yaml.safe_dump({"sprint_name": "S"}, allow_unicode=True), encoding="utf-8"
    )
    mock_sync = MagicMock(spec=ProgressSyncService)
    mock_sync.repo_root = tmp_path

    mock_code = MagicMock(spec=CodeWriteService)
    executor = ReviewerExecutor(
        sprint_state_service=SprintStateService(tmp_path, status_file),
        progress_sync_service=mock_sync,
        project_id="test-project",
        code_write_service=mock_code,
        role_artifact_writer=role_artifact_writer,
    )
    return executor, mock_code


def test_reviewer_exposes_three_read_only_code_tools_when_wired(tmp_path: Path):
    """With a CodeWriteService injected, the reviewer surfaces exactly
    the three read-only code tools — nothing writable."""
    executor, _ = _make_reviewer_with_code_service(tmp_path)
    names = {s.name for s in executor.tool_specs()}
    # Static reviewer tools still present
    assert {"read_sprint_status", "read_bitable_rows", "read_bitable_schema"} <= names
    # Read-only code surface fully present
    assert REVIEWER_CODE_READ_ALLOW <= names


def test_reviewer_never_exposes_writes_or_git_even_with_service(tmp_path: Path):
    """Allow-filter is the backstop: even with CodeWriteService AND a
    CIWatchService wired, write / commit / push / PR / inspect / CI-watch
    tools stay invisible."""
    from feishu_agent.tools.ci_watch_service import CIWatchService

    executor, _ = _make_reviewer_with_code_service(tmp_path)
    # Sneak a CIWatchService past the constructor — simulates a future
    # wiring bug that forgets to null it out for non-TL roles.
    executor._ci_watch = MagicMock(spec=CIWatchService)

    names = {s.name for s in executor.tool_specs()}
    forbidden = {
        "write_project_code",
        "write_project_code_batch",
        "git_commit",
        "git_sync_remote",
        "git_push",
        "create_pull_request",
        "run_pre_push_inspection",
        "watch_pr_checks",
    }
    assert forbidden.isdisjoint(names), (
        f"Reviewer leaked forbidden tools: {forbidden & names}"
    )


def test_reviewer_no_code_tools_without_service(tmp_path: Path):
    """If no CodeWriteService is wired, even the read-only surface stays
    empty. This matches the original reviewer behavior — adding the
    read surface is purely additive."""
    executor = _make_executor(tmp_path)
    names = {s.name for s in executor.tool_specs()}
    assert REVIEWER_CODE_READ_ALLOW.isdisjoint(names)


@pytest.mark.asyncio
async def test_reviewer_read_project_code_delegates_to_service(tmp_path: Path):
    executor, mock_code = _make_reviewer_with_code_service(tmp_path)
    mock_code.read_source.return_value = {
        "relative_path": "docs/implementation/3-1-impl.md",
        "content": "# Impl note",
        "bytes_read": 12,
    }
    result = await executor.execute_tool(
        "read_project_code",
        {"relative_path": "docs/implementation/3-1-impl.md"},
    )
    assert result["content"] == "# Impl note"
    mock_code.read_source.assert_called_once()


@pytest.mark.asyncio
async def test_reviewer_list_project_paths_delegates_to_service(tmp_path: Path):
    executor, mock_code = _make_reviewer_with_code_service(tmp_path)
    mock_code.list_paths.return_value = {
        "sub_path": "docs",
        "entries": [{"name": "implementation", "is_dir": True}],
    }
    result = await executor.execute_tool(
        "list_project_paths", {"sub_path": "docs"}
    )
    assert result["entries"][0]["name"] == "implementation"
    mock_code.list_paths.assert_called_once()


@pytest.mark.asyncio
async def test_reviewer_describe_policy_delegates(tmp_path: Path):
    executor, mock_code = _make_reviewer_with_code_service(tmp_path)
    mock_code.describe_policy.return_value = {
        "project_id": "test-project",
        "allowed_write_roots": ["src/"],
    }
    result = await executor.execute_tool(
        "describe_code_write_policy", {}
    )
    assert result["project_id"] == "test-project"
    mock_code.describe_policy.assert_called_once_with("test-project")


@pytest.mark.asyncio
async def test_reviewer_write_project_code_refused_at_dispatch(tmp_path: Path):
    """Belt + suspenders: even if an LLM somehow calls write_project_code
    (stale context, hallucination), dispatch refuses it before touching
    the service."""
    executor, mock_code = _make_reviewer_with_code_service(tmp_path)
    result = await executor.execute_tool(
        "write_project_code",
        {
            "relative_path": "src/evil.py",
            "content": "import os",
            "reason": "test",
        },
    )
    assert isinstance(result, dict)
    assert result.get("error") == "TOOL_NOT_ALLOWED_ON_ROLE"
    # Crucially, write_source must NOT have been called.
    assert not mock_code.write_source.called


@pytest.mark.asyncio
async def test_reviewer_git_push_refused_at_dispatch(tmp_path: Path):
    """Same defense-in-depth for gatekeeper tools."""
    executor, _ = _make_reviewer_with_code_service(tmp_path)
    result = await executor.execute_tool(
        "git_push", {"inspection_token": "fake"}
    )
    assert isinstance(result, dict)
    assert result.get("error") == "TOOL_NOT_ALLOWED_ON_ROLE"


@pytest.mark.asyncio
async def test_reviewer_watch_pr_checks_refused_at_dispatch(tmp_path: Path):
    """Post-PR CI gate is TL-only. Reviewer must never be able to
    declare PR merge-readiness, even if a stale LLM context names the
    tool. We force-wire the service to prove the filter runs BEFORE the
    service is touched."""
    from feishu_agent.tools.ci_watch_service import CIWatchService

    executor, _ = _make_reviewer_with_code_service(tmp_path)
    executor._ci_watch = MagicMock(spec=CIWatchService)

    result = await executor.execute_tool(
        "watch_pr_checks", {"pr_number": 1}
    )
    assert isinstance(result, dict)
    assert result.get("error") == "TOOL_NOT_ALLOWED_ON_ROLE"
    executor._ci_watch.watch.assert_not_called()


# ======================================================================
# Frontmatter parity
# ======================================================================


def test_tool_allow_list_matches_role_file(tmp_path: Path):
    """skill doc's ``tool_allow_list`` must equal the tool surface the
    executor actually produces when fully wired (role artifact writer +
    code-read service)."""
    registry = RoleRegistryService(ROLES_DIR)
    role = registry.get_role("reviewer")
    writer = RoleArtifactWriter(
        role_name="reviewer",
        project_id="p",
        allowed_write_root=tmp_path,
    )
    from feishu_agent.tools.workflow_service import WorkflowService

    executor = ReviewerExecutor(
        sprint_state_service=MagicMock(spec=SprintStateService),
        progress_sync_service=MagicMock(spec=ProgressSyncService),
        role_artifact_writer=writer,
        code_write_service=MagicMock(spec=CodeWriteService),
        workflow_service=MagicMock(spec=WorkflowService),
    )
    assert set(role.tool_allow_list) == {s.name for s in executor.tool_specs()}
