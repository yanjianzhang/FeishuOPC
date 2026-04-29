from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from feishu_agent.roles.role_executors.progress_sync import (
    PROGRESS_SYNC_TOOL_SPECS,
    ProgressSyncExecutor,
)
from feishu_agent.roles.role_registry_service import RoleRegistryService
from feishu_agent.team.sprint_state_service import SprintStateService
from feishu_agent.tools.progress_sync_service import ProgressSyncService

ROLES_DIR = Path(__file__).resolve().parents[3] / "skills" / "roles"


def _make_executor(
    tmp_path: Path,
    *,
    bitable_tables: dict[str, Any] | None = None,
    role_permissions: list[Any] | None = None,
) -> ProgressSyncExecutor:
    status_file = "sprint-status.yaml"
    (tmp_path / status_file).write_text(
        yaml.safe_dump({"sprint_name": "Sprint 5", "current_sprint": {"goal": "Sync"}}, allow_unicode=True),
        encoding="utf-8",
    )

    mock_sync = MagicMock(spec=ProgressSyncService)
    mock_sync.repo_root = tmp_path

    return ProgressSyncExecutor(
        sprint_state_service=SprintStateService(tmp_path, status_file),
        progress_sync_service=mock_sync,
        project_id="test-project",
        command_text="test command",
        trace_id="trace-test",
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
    assert names == {"preview_progress_sync", "write_progress_sync", "resolve_bitable_target"}


def test_tool_specs_returns_fresh_list(tmp_path: Path):
    executor = _make_executor(tmp_path)
    a = executor.tool_specs()
    b = executor.tool_specs()
    assert a is not b
    assert a == b


def test_module_level_specs_match_instance(tmp_path: Path):
    executor = _make_executor(tmp_path)
    assert [s.name for s in executor.tool_specs()] == [s.name for s in PROGRESS_SYNC_TOOL_SPECS]


# ======================================================================
# execute_tool handlers
# ======================================================================


@pytest.mark.asyncio
async def test_resolve_bitable_target_no_candidates(tmp_path: Path):
    executor = _make_executor(tmp_path)
    result = await executor.execute_tool("resolve_bitable_target", {})
    assert result["resolved"] is False
    assert result["table_name"] is None


@pytest.mark.asyncio
async def test_resolve_bitable_target_with_candidates(tmp_path: Path):
    perm = MagicMock()
    perm.table_name = "progress_table"
    perm.can_read = True
    perm.can_write = True
    perm.is_default = True
    table_obj = MagicMock()
    table_obj.notes = "Main progress table"

    executor = _make_executor(
        tmp_path,
        bitable_tables={"progress_table": table_obj},
        role_permissions=[perm],
    )
    result = await executor.execute_tool("resolve_bitable_target", {"require_write": False})
    assert result["resolved"] is True
    assert result["table_name"] == "progress_table"
    assert result["notes"] == "Main progress table"


@pytest.mark.asyncio
async def test_preview_progress_sync_happy_path(tmp_path: Path):
    perm = MagicMock()
    perm.table_name = "progress_table"
    perm.can_read = True
    perm.can_write = False
    perm.is_default = True
    table_obj = MagicMock()

    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.mode = "preview"
    mock_response.message = "Previewed 5 records"
    mock_response.summary = MagicMock()
    mock_response.summary.model_dump.return_value = {"total": 5, "by_status": {"done": 3, "in-progress": 2}}
    mock_response.warnings = []
    mock_response.errors = []
    mock_response.write_result = None

    mock_service = MagicMock(spec=ProgressSyncService)
    mock_service.execute = AsyncMock(return_value=mock_response)

    executor = _make_executor(
        tmp_path,
        bitable_tables={"progress_table": table_obj},
        role_permissions=[perm],
    )
    executor._build_progress_sync_service_for_target = lambda _tn, _bt: mock_service

    result = await executor.execute_tool("preview_progress_sync", {})
    assert result["ok"] is True
    assert result["mode"] == "preview"
    assert result["target_table_name"] == "progress_table"
    assert result["summary"]["total"] == 5


@pytest.mark.asyncio
async def test_write_progress_sync_happy_path(tmp_path: Path):
    perm = MagicMock()
    perm.table_name = "progress_table"
    perm.can_read = True
    perm.can_write = True
    perm.is_default = True
    table_obj = MagicMock()

    mock_write_result = MagicMock()
    mock_write_result.created = 2
    mock_write_result.updated = 3
    mock_write_result.skipped = 0
    mock_write_result.failed = 0

    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.mode = "write"
    mock_response.message = "Synced 5 records"
    mock_response.summary = MagicMock()
    mock_response.summary.model_dump.return_value = {"total": 5, "by_status": {"done": 5}}
    mock_response.warnings = []
    mock_response.errors = []
    mock_response.write_result = mock_write_result

    mock_service = MagicMock(spec=ProgressSyncService)
    mock_service.execute = AsyncMock(return_value=mock_response)

    executor = _make_executor(
        tmp_path,
        bitable_tables={"progress_table": table_obj},
        role_permissions=[perm],
    )
    executor._build_progress_sync_service_for_target = lambda _tn, _bt: mock_service

    result = await executor.execute_tool("write_progress_sync", {})
    assert result["ok"] is True
    assert result["mode"] == "write"
    assert result["target_table_name"] == "progress_table"
    assert result["write_result"]["created"] == 2
    assert result["write_result"]["updated"] == 3


@pytest.mark.asyncio
async def test_unsupported_tool_raises(tmp_path: Path):
    executor = _make_executor(tmp_path)
    with pytest.raises(RuntimeError, match="Unsupported tool"):
        await executor.execute_tool("nonexistent", {})


# ======================================================================
# Frontmatter parity
# ======================================================================


def test_tool_allow_list_matches_role_file():
    registry = RoleRegistryService(ROLES_DIR)
    role = registry.get_role("progress_sync")
    executor = ProgressSyncExecutor(
        sprint_state_service=MagicMock(spec=SprintStateService),
        progress_sync_service=MagicMock(spec=ProgressSyncService),
    )
    assert set(role.tool_allow_list) == {s.name for s in executor.tool_specs()}
