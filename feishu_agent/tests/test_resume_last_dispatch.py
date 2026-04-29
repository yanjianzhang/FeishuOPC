"""Tests for ``TechLeadToolExecutor._resume_last_dispatch``.

The resume tool is a pure bookkeeping wrapper: it walks the thread's
append-only event log, finds the latest ``dispatch_role_agent`` call,
reconstructs its args, and re-dispatches the same role with a task
body that tells the new sub-agent to read any partial artifacts left
on disk and continue from there.

These tests pin the four behaviors we care about:

1. With no ``TaskHandle`` wired, the tool short-circuits with
   ``NO_TASK_HANDLE`` — it must not crash or emit a dispatch.
2. With a fresh (empty) log the tool returns ``NO_PRIOR_DISPATCH``;
   again no dispatch is made.
3. When a prior dispatch exists and its last result was an error,
   the resume path composes an augmented task and calls through to
   ``_dispatch_role_agent`` with the SAME role / workflow_id as
   before (the LLM does not have to retype anything).
4. When the last dispatch succeeded, the tool refuses by default
   (``LAST_DISPATCH_SUCCEEDED``) and only re-dispatches when the
   caller passes ``force=true``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from feishu_agent.core.llm_agent_adapter import LlmAgentAdapter
from feishu_agent.roles.role_registry_service import RoleRegistryService
from feishu_agent.roles.tech_lead_executor import (
    ResumeLastDispatchArgs,
    TechLeadToolExecutor,
)
from feishu_agent.team.audit_service import AuditService
from feishu_agent.team.sprint_state_service import SprintStateService
from feishu_agent.team.task_event_log import TaskKey
from feishu_agent.team.task_service import TaskService
from feishu_agent.tools.progress_sync_service import ProgressSyncService


def _make_executor(
    tmp_path: Path,
    *,
    task_handle: Any = None,
) -> TechLeadToolExecutor:
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir(exist_ok=True)
    audit_dir = tmp_path / "audit"
    status_file = "sprint-status.yaml"

    mock_sync = MagicMock(spec=ProgressSyncService)
    mock_sync.repo_root = tmp_path
    mock_llm = MagicMock(spec=LlmAgentAdapter)

    return TechLeadToolExecutor(
        progress_sync_service=mock_sync,
        sprint_state_service=SprintStateService(tmp_path, status_file),
        audit_service=AuditService(audit_dir),
        llm_agent_adapter=mock_llm,
        role_registry=RoleRegistryService(roles_dir),
        project_id="test-project",
        command_text="test command",
        trace_id="trace-001",
        chat_id="chat-001",
        task_handle=task_handle,
    )


def _open_handle(tmp_path: Path) -> Any:
    svc = TaskService(tmp_path / "tasks")
    return svc.open_or_resume(
        TaskKey(bot_name="tech_lead", chat_id="chat-1", root_id="root-1"),
        role_name="tech_lead",
        project_id="test-project",
    )


def _log_dispatch_call(
    handle: Any,
    *,
    call_id: str,
    role_name: str,
    task: str,
    workflow_id: str = "",
    acceptance_criteria: str = "",
) -> None:
    """Write a ``tool.call`` event mirroring what ``LlmAgentAdapter`` emits."""
    args = {
        "role_name": role_name,
        "task": task,
        "acceptance_criteria": acceptance_criteria,
        "workflow_id": workflow_id,
    }
    handle.append(
        kind="tool.call",
        payload={
            "tool_name": "dispatch_role_agent",
            "call_id": call_id,
            "args_preview": json.dumps(args, ensure_ascii=False, default=str),
        },
    )


def _log_tool_error(handle: Any, *, call_id: str, error: str) -> None:
    handle.append(
        kind="tool.error",
        payload={
            "tool_name": "dispatch_role_agent",
            "call_id": call_id,
            "result_preview": json.dumps({"error": error}),
        },
    )


def _log_tool_ok(handle: Any, *, call_id: str, output: dict[str, Any]) -> None:
    handle.append(
        kind="tool.result",
        payload={
            "tool_name": "dispatch_role_agent",
            "call_id": call_id,
            "result_preview": json.dumps({"success": True, **output}),
        },
    )


# ----------------------------------------------------------------------
# 1. NO_TASK_HANDLE short-circuit
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_without_task_handle_returns_structured_error(tmp_path: Path):
    executor = _make_executor(tmp_path, task_handle=None)
    result = await executor._resume_last_dispatch(ResumeLastDispatchArgs())
    assert result == {
        "ok": False,
        "error": "NO_TASK_HANDLE",
        "message": (
            "resume_last_dispatch requires per-thread task "
            "logging, which is not wired in this session. "
            "Re-issue the dispatch manually with "
            "dispatch_role_agent."
        ),
    }


def test_resume_tool_hidden_from_specs_when_no_task_handle(tmp_path: Path):
    executor = _make_executor(tmp_path, task_handle=None)
    names = {s.name for s in executor.tool_specs()}
    assert "resume_last_dispatch" not in names


def test_resume_tool_visible_when_task_handle_wired(tmp_path: Path):
    handle = _open_handle(tmp_path)
    executor = _make_executor(tmp_path, task_handle=handle)
    names = {s.name for s in executor.tool_specs()}
    assert "resume_last_dispatch" in names


# ----------------------------------------------------------------------
# 2. NO_PRIOR_DISPATCH on empty log
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_without_prior_dispatch_returns_structured_error(tmp_path: Path):
    handle = _open_handle(tmp_path)
    executor = _make_executor(tmp_path, task_handle=handle)
    result = await executor._resume_last_dispatch(ResumeLastDispatchArgs())
    assert result["ok"] is False
    assert result["error"] == "NO_PRIOR_DISPATCH"


# ----------------------------------------------------------------------
# 3. Happy path: last dispatch errored → re-dispatch with same role
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_after_error_re_dispatches_same_role_and_workflow(tmp_path: Path):
    handle = _open_handle(tmp_path)
    _log_dispatch_call(
        handle,
        call_id="call-1",
        role_name="reviewer",
        task="对 story 3-3 做 code-review。",
        workflow_id="bmad:code-review",
        acceptance_criteria="产出 reviews/3-3-code-review.md",
    )
    _log_tool_error(handle, call_id="call-1", error="Tool loop timed out after 177s")

    executor = _make_executor(tmp_path, task_handle=handle)
    # Bypass the real sub-agent spawn — we only care that resume
    # called _dispatch_role_agent with the reconstructed args.
    captured: dict[str, Any] = {}

    async def _fake_dispatch(args: Any) -> dict[str, Any]:
        captured["args"] = args
        return {"role_name": args.role_name, "success": True, "output": "mocked"}

    executor._dispatch_role_agent = _fake_dispatch  # type: ignore[assignment]

    result = await executor._resume_last_dispatch(
        ResumeLastDispatchArgs(extra_context="只补全缺失的风险评估章节。")
    )

    assert result["ok"] is True
    assert result["resumed_from"]["role_name"] == "reviewer"
    assert result["resumed_from"]["workflow_id"] == "bmad:code-review"
    assert result["resumed_from"]["last_result_kind"] == "tool.error"
    # The forwarded dispatch carries the same role_name / workflow_id
    # as the prior call, and its task body includes the resume header
    # plus the user's extra_context so the sub-agent can pick up.
    forwarded = captured["args"]
    assert forwarded.role_name == "reviewer"
    assert forwarded.workflow_id == "bmad:code-review"
    assert "【续跑任务】" in forwarded.task
    assert "对 story 3-3 做 code-review" in forwarded.task
    assert "只补全缺失的风险评估章节" in forwarded.task


# ----------------------------------------------------------------------
# 4. Refuse-when-succeeded / force override
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_refuses_when_last_dispatch_succeeded(tmp_path: Path):
    handle = _open_handle(tmp_path)
    _log_dispatch_call(
        handle,
        call_id="call-ok",
        role_name="reviewer",
        task="前一次成功的 code-review。",
        workflow_id="bmad:code-review",
    )
    _log_tool_ok(handle, call_id="call-ok", output={"artifact": "reviews/ok.md"})

    executor = _make_executor(tmp_path, task_handle=handle)
    executor._dispatch_role_agent = AsyncMock()  # type: ignore[assignment]

    result = await executor._resume_last_dispatch(ResumeLastDispatchArgs())
    assert result["ok"] is False
    assert result["error"] == "LAST_DISPATCH_SUCCEEDED"
    executor._dispatch_role_agent.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_resume_force_overrides_success_gate(tmp_path: Path):
    handle = _open_handle(tmp_path)
    _log_dispatch_call(
        handle,
        call_id="call-ok",
        role_name="developer",
        task="已完成的开发任务。",
        workflow_id="bmad:dev-story",
    )
    _log_tool_ok(handle, call_id="call-ok", output={"artifact": "stories/ok.md"})

    executor = _make_executor(tmp_path, task_handle=handle)
    captured: dict[str, Any] = {}

    async def _fake_dispatch(args: Any) -> dict[str, Any]:
        captured["args"] = args
        return {"role_name": args.role_name, "success": True}

    executor._dispatch_role_agent = _fake_dispatch  # type: ignore[assignment]

    result = await executor._resume_last_dispatch(
        ResumeLastDispatchArgs(force=True)
    )
    assert result["ok"] is True
    assert captured["args"].role_name == "developer"
    assert captured["args"].workflow_id == "bmad:dev-story"
