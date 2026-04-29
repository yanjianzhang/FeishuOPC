"""Tests for the confirmation flow: request_confirmation tool + pending state resume/cancel."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import yaml
from feishu_agent.core.llm_gateway_shim import MockGateway as _BaseMockGateway

from feishu_agent.core.llm_agent_adapter import LlmAgentAdapter
from feishu_agent.roles.tech_lead_executor import (
    TECH_LEAD_V2_TOOL_SPECS,
    TechLeadToolExecutor,
)
from feishu_agent.runtime.feishu_runtime_service import (
    FeishuBotContext,
    _execute_force_sync_pending,
    _execute_pending_action,
    _reset_runtime_caches,
    process_role_message,
)
from feishu_agent.team.pending_action_service import PendingAction, PendingActionService
from feishu_agent.tools.project_registry import build_project_registry

# ======================================================================
# Fixtures
# ======================================================================


def _make_executor(tmp_path: Path, *, pending_service: PendingActionService | None = None) -> TechLeadToolExecutor:
    from feishu_agent.roles.role_registry_service import RoleRegistryService
    from feishu_agent.team.audit_service import AuditService
    from feishu_agent.team.sprint_state_service import SprintStateService
    from feishu_agent.tools.progress_sync_service import ProgressSyncService

    roles_dir = tmp_path / "roles"
    roles_dir.mkdir(exist_ok=True)
    status_path = tmp_path / "sprint-status.yaml"
    status_path.write_text(yaml.safe_dump({"sprint_name": "S5", "current_sprint": {"goal": "test"}}), encoding="utf-8")

    mock_sync = MagicMock(spec=ProgressSyncService)
    mock_sync.repo_root = tmp_path
    mock_llm = MagicMock(spec=LlmAgentAdapter)

    return TechLeadToolExecutor(
        progress_sync_service=mock_sync,
        sprint_state_service=SprintStateService(tmp_path, "sprint-status.yaml"),
        audit_service=AuditService(tmp_path / "audit"),
        llm_agent_adapter=mock_llm,
        role_registry=RoleRegistryService(roles_dir),
        pending_action_service=pending_service,
        project_id="test",
        command_text="test",
        trace_id="trace-test",
        chat_id="chat-test",
    )


# ======================================================================
# T094a — request_confirmation tool on TechLead
# ======================================================================


def test_tool_specs_include_request_confirmation():
    names = {s.name for s in TECH_LEAD_V2_TOOL_SPECS}
    assert "request_confirmation" in names


def test_request_confirmation_schema_has_required_fields():
    spec = next(s for s in TECH_LEAD_V2_TOOL_SPECS if s.name == "request_confirmation")
    props = spec.input_schema.get("properties", {})
    assert "action_type" in props
    assert "action_args" in props
    assert "summary" in props


@pytest.mark.asyncio
async def test_request_confirmation_stores_pending_action(tmp_path: Path):
    pending_dir = tmp_path / "pending"
    pending_service = PendingActionService(pending_dir)
    executor = _make_executor(tmp_path, pending_service=pending_service)

    result = await executor.execute_tool("request_confirmation", {
        "action_type": "write_progress_sync",
        "action_args": {"module": "vineyard_module"},
        "summary": "将写入 vineyard 模块进度到飞书多维表格",
    })

    assert result["stored"] is True
    assert result["action_type"] == "write_progress_sync"
    assert "pending_trace_id" in result

    loaded = pending_service.load(result["pending_trace_id"])
    assert loaded is not None
    assert loaded.chat_id == "chat-test"
    assert loaded.action_type == "write_progress_sync"
    assert loaded.action_args == {"module": "vineyard_module"}


@pytest.mark.asyncio
async def test_request_confirmation_without_service_returns_error(tmp_path: Path):
    executor = _make_executor(tmp_path, pending_service=None)

    result = await executor.execute_tool("request_confirmation", {
        "action_type": "write_progress_sync",
        "action_args": {},
        "summary": "测试",
    })

    assert result["stored"] is False
    assert "not configured" in result["error"]


@pytest.mark.asyncio
async def test_request_confirmation_includes_instruction(tmp_path: Path):
    pending_dir = tmp_path / "pending"
    executor = _make_executor(tmp_path, pending_service=PendingActionService(pending_dir))

    result = await executor.execute_tool("request_confirmation", {
        "action_type": "write_progress_sync",
        "action_args": {"module": "vineyard_module"},
        "summary": "将 vineyard_module 的进度写入 Bitable",
    })

    assert "确认" in result["instruction"]
    assert "取消" in result["instruction"]


@pytest.mark.asyncio
async def test_request_confirmation_rejects_advance_sprint_state(tmp_path: Path):
    """Contract: ``advance_sprint_state`` must NOT be a valid
    ``action_type``. Status flips are reversible + audit-logged, so
    gating them behind a confirm round-trip just created UX friction
    (users typing "推进下一个 sprint" would hit pending_reminder
    loops from stale gates). The Pydantic Literal enforces this —
    Call validation raises, it never reaches the executor body.
    """
    from pydantic import ValidationError

    pending_dir = tmp_path / "pending"
    executor = _make_executor(tmp_path, pending_service=PendingActionService(pending_dir))

    with pytest.raises(ValidationError):
        await executor.execute_tool("request_confirmation", {
            "action_type": "advance_sprint_state",
            "action_args": {"story_key": "3-1", "to_status": "review"},
            "summary": "不应该被允许",
        })


def test_request_confirmation_args_literal_excludes_advance_sprint_state():
    """Direct schema assertion so Pydantic-level changes don't sneak
    the gate back in without tripping CI.
    """
    from pydantic import ValidationError

    from feishu_agent.tools.feishu_agent_tools import RequestConfirmationArgs

    RequestConfirmationArgs(
        action_type="write_progress_sync",
        action_args={"module": "vineyard_module"},
        summary="ok",
    )

    with pytest.raises(ValidationError):
        RequestConfirmationArgs(
            action_type="advance_sprint_state",  # type: ignore[arg-type]
            action_args={},
            summary="blocked",
        )


# ======================================================================
# T094b — pending state in process_role_message()
# ======================================================================


class HttpOnlyMockGateway(_BaseMockGateway):
    async def subscribe(self, event_types=None):
        raise NotImplementedError


def _build_chat_response(content: str = "ok") -> dict:
    return {
        "runId": str(uuid.uuid4()),
        "content": content,
        "status": "completed",
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


def _bot_context() -> FeishuBotContext:
    return FeishuBotContext(
        bot_name="tech_lead",
        app_id="app-test",
        app_secret="secret-test",
        verification_token=None,
        encrypt_key=None,
    )


def _build_repo_fixture(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    roles_dir = repo_root / "skills" / "roles"
    roles_dir.mkdir(parents=True)

    prompt_path = repo_root / "skills" / "tech_lead.md"
    prompt_path.write_text("你是技术组长。", encoding="utf-8")

    (roles_dir / "sprint_planner.md").write_text(
        "---\ntags: [plan]\ntool_allow_list: [read_sprint_status]\n---\nYou plan sprints.",
        encoding="utf-8",
    )

    adapter_dir = repo_root / "project-adapters"
    adapter_dir.mkdir()
    import json
    (adapter_dir / "exampleapp-progress.json").write_text(
        json.dumps(
            {
                "project_id": "exampleapp",
                "display_name": "ExampleApp",
                "source_roots": {"status_file": "sprint-status.yaml"},
            }
        ),
        encoding="utf-8",
    )

    # Explicit project registry so the runtime resolves a default project.
    projects_dir = repo_root / ".larkagent" / "secrets" / "projects"
    projects_dir.mkdir(parents=True)
    (projects_dir / "projects.jsonl").write_text(
        '{"project_id":"exampleapp","is_default":true}\n',
        encoding="utf-8",
    )

    status_path = repo_root / "sprint-status.yaml"
    status_path.write_text(
        yaml.safe_dump({"sprint_name": "Sprint 5", "current_sprint": {"goal": "Integration"}}),
        encoding="utf-8",
    )

    log_dir = repo_root / "server" / "data" / "techbot-runs"
    log_dir.mkdir(parents=True)

    return repo_root


@pytest_asyncio.fixture()
async def llm_agent_mock():
    mock_gw = HttpOnlyMockGateway()
    mock_gw.register("agents.create", lambda p: {"agentId": p.get("agentId", "test"), "status": "created"})
    mock_gw.register("chat.send", lambda p: _build_chat_response("TechLead 回复"))
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
    return adapter


@pytest.mark.asyncio
async def test_confirm_pending_action(tmp_path: Path, llm_agent_mock, monkeypatch):
    repo_root = _build_repo_fixture(tmp_path)
    monkeypatch.setattr("feishu_agent.runtime.feishu_runtime_service.settings.app_repo_root", str(repo_root))

    pending_dir = repo_root / "data" / "techbot-runs" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    action = PendingAction(
        trace_id="pending-abc123",
        chat_id="chat-001",
        role_name="tech_lead",
        action_type="write_progress_sync",
        action_args={"module": None},
    )
    PendingActionService(pending_dir).save(action)

    mock_execute = AsyncMock()
    mock_result = MagicMock()
    mock_result.ok = True
    mock_result.message = "同步完成"
    mock_result.mode = "write"
    mock_result.summary = MagicMock()
    mock_result.summary.model_dump.return_value = {}
    mock_result.warnings = []
    mock_result.errors = []
    mock_result.write_result = None
    mock_execute.return_value = mock_result
    monkeypatch.setattr(
        "feishu_agent.runtime.feishu_runtime_service.build_progress_sync_service",
        lambda ctx, **kw: MagicMock(execute=mock_execute),
    )

    result = await process_role_message(
        role_name="tech-lead-planner",
        command_text="确认",
        trace_id="trace-confirm",
        chat_id="chat-001",
        bot_context=_bot_context(),
        llm_agent_adapter=llm_agent_mock,
    )

    assert result.ok is True
    assert result.route_action == "pending_executed"
    assert not (pending_dir / "pending-abc123.json").exists()


@pytest.mark.asyncio
async def test_cancel_pending_action(tmp_path: Path, llm_agent_mock, monkeypatch):
    repo_root = _build_repo_fixture(tmp_path)
    monkeypatch.setattr("feishu_agent.runtime.feishu_runtime_service.settings.app_repo_root", str(repo_root))

    pending_dir = repo_root / "data" / "techbot-runs" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    action = PendingAction(
        trace_id="pending-xyz789",
        chat_id="chat-002",
        role_name="tech_lead",
        action_type="write_progress_sync",
        action_args={"module": "vineyard_module"},
    )
    PendingActionService(pending_dir).save(action)

    result = await process_role_message(
        role_name="tech-lead-planner",
        command_text="取消",
        trace_id="trace-cancel",
        chat_id="chat-002",
        bot_context=_bot_context(),
        llm_agent_adapter=llm_agent_mock,
    )

    assert result.ok is True
    assert result.route_action == "pending_cancelled"
    assert "取消" in result.message
    assert not (pending_dir / "pending-xyz789.json").exists()


@pytest.mark.asyncio
async def test_pending_reminder_on_other_message(tmp_path: Path, llm_agent_mock, monkeypatch):
    repo_root = _build_repo_fixture(tmp_path)
    monkeypatch.setattr("feishu_agent.runtime.feishu_runtime_service.settings.app_repo_root", str(repo_root))

    pending_dir = repo_root / "data" / "techbot-runs" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    action = PendingAction(
        trace_id="pending-remind",
        chat_id="chat-003",
        role_name="tech_lead",
        action_type="write_progress_sync",
        action_args={},
    )
    PendingActionService(pending_dir).save(action)

    result = await process_role_message(
        role_name="tech-lead-planner",
        command_text="查看 sprint 状态",
        trace_id="trace-other",
        chat_id="chat-003",
        bot_context=_bot_context(),
        llm_agent_adapter=llm_agent_mock,
    )

    assert result.ok is True
    assert result.route_action == "pending_reminder"
    assert "待确认" in result.message
    assert (pending_dir / "pending-remind.json").exists()


@pytest.mark.asyncio
async def test_no_pending_proceeds_normally(tmp_path: Path, llm_agent_mock, monkeypatch):
    """When no pending action, process_role_message runs the normal role LLM session."""
    repo_root = _build_repo_fixture(tmp_path)
    monkeypatch.setattr("feishu_agent.runtime.feishu_runtime_service.settings.app_repo_root", str(repo_root))

    result = await process_role_message(
        role_name="tech-lead-planner",
        command_text="查看 Sprint 状态",
        trace_id="trace-normal",
        chat_id="chat-normal",
        bot_context=_bot_context(),
        llm_agent_adapter=llm_agent_mock,
    )

    assert result.ok is True
    assert result.route_action == "role_llm_session"


@pytest.mark.asyncio
async def test_confirmation_keywords_case_insensitive(tmp_path: Path, llm_agent_mock, monkeypatch):
    repo_root = _build_repo_fixture(tmp_path)
    monkeypatch.setattr("feishu_agent.runtime.feishu_runtime_service.settings.app_repo_root", str(repo_root))

    pending_dir = repo_root / "data" / "techbot-runs" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    action = PendingAction(
        trace_id="pending-case",
        chat_id="chat-case",
        role_name="tech_lead",
        action_type="write_progress_sync",
        action_args={},
    )
    PendingActionService(pending_dir).save(action)

    monkeypatch.setattr(
        "feishu_agent.runtime.feishu_runtime_service.build_progress_sync_service",
        lambda ctx, **kw: MagicMock(execute=AsyncMock(return_value=MagicMock(ok=True, message="done"))),
    )

    result = await process_role_message(
        role_name="tech-lead-planner",
        command_text="YES",
        trace_id="trace-case",
        chat_id="chat-case",
        bot_context=_bot_context(),
        llm_agent_adapter=llm_agent_mock,
    )

    assert result.route_action == "pending_executed"


@pytest.mark.asyncio
async def test_confirm_pending_action_strips_leading_mention(tmp_path: Path, llm_agent_mock, monkeypatch):
    """Regression: Feishu group-chat replies always arrive with a leading
    ``@_user_N`` mention placeholder (e.g. ``@_user_1 确认``). Before the
    fix, that placeholder made keyword matching fail and the user got
    stuck in the pending-reminder loop even after typing ``确认``.
    """
    repo_root = _build_repo_fixture(tmp_path)
    monkeypatch.setattr("feishu_agent.runtime.feishu_runtime_service.settings.app_repo_root", str(repo_root))

    pending_dir = repo_root / "data" / "techbot-runs" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    action = PendingAction(
        trace_id="pending-mention",
        chat_id="chat-mention",
        role_name="tech_lead",
        action_type="write_progress_sync",
        action_args={},
    )
    PendingActionService(pending_dir).save(action)

    monkeypatch.setattr(
        "feishu_agent.runtime.feishu_runtime_service.build_progress_sync_service",
        lambda ctx, **kw: MagicMock(
            execute=AsyncMock(return_value=MagicMock(ok=True, message="done"))
        ),
    )

    result = await process_role_message(
        role_name="tech-lead-planner",
        command_text="@_user_1 确认",
        trace_id="trace-mention",
        chat_id="chat-mention",
        bot_context=_bot_context(),
        llm_agent_adapter=llm_agent_mock,
    )

    assert result.route_action == "pending_executed"
    assert not (pending_dir / "pending-mention.json").exists()


@pytest.mark.asyncio
async def test_cancel_pending_action_strips_leading_mention(tmp_path: Path, llm_agent_mock, monkeypatch):
    """Symmetric to the confirm case: ``@_user_2 取消`` must cancel."""
    repo_root = _build_repo_fixture(tmp_path)
    monkeypatch.setattr("feishu_agent.runtime.feishu_runtime_service.settings.app_repo_root", str(repo_root))

    pending_dir = repo_root / "data" / "techbot-runs" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    action = PendingAction(
        trace_id="pending-mention-cancel",
        chat_id="chat-mention-cancel",
        role_name="tech_lead",
        action_type="write_progress_sync",
        action_args={"module": "vineyard_module"},
    )
    PendingActionService(pending_dir).save(action)

    result = await process_role_message(
        role_name="tech-lead-planner",
        command_text="@_user_2 取消",
        trace_id="trace-mention-cancel",
        chat_id="chat-mention-cancel",
        bot_context=_bot_context(),
        llm_agent_adapter=llm_agent_mock,
    )

    assert result.route_action == "pending_cancelled"
    assert not (pending_dir / "pending-mention-cancel.json").exists()


@pytest.mark.asyncio
async def test_stale_advance_sprint_state_pending_is_silently_cleaned(
    tmp_path: Path, llm_agent_mock, monkeypatch
):
    """After removing the ``advance_sprint_state`` gate, any pending
    file with that action type on disk is stale (written by an older
    build). It must be silently deleted on the next incoming
    message so the user's fresh intent is NOT hijacked by an old
    "当前有待确认的操作：advance_sprint_state" reminder loop.

    This also guards against the original SV-prod symptom: stale
    pending → reminder → user types "确认" → boom.
    """
    repo_root = _build_repo_fixture(tmp_path)
    monkeypatch.setattr(
        "feishu_agent.runtime.feishu_runtime_service.settings.app_repo_root",
        str(repo_root),
    )

    pending_dir = repo_root / "data" / "techbot-runs" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    action = PendingAction(
        trace_id="pending-stale-advance",
        chat_id="chat-stale",
        role_name="tech_lead",
        action_type="advance_sprint_state",
        action_args={"story_key": "x", "to_status": "review"},
    )
    PendingActionService(pending_dir).save(action)
    assert (pending_dir / "pending-stale-advance.json").exists()

    result = await process_role_message(
        role_name="tech-lead-planner",
        command_text="推进下一个sprint",
        trace_id="trace-stale",
        chat_id="chat-stale",
        bot_context=_bot_context(),
        llm_agent_adapter=llm_agent_mock,
    )

    assert result.ok is True
    # Falls through to the normal LLM session — NOT pending_reminder
    # and NOT pending_executed. User's fresh message is handled.
    assert result.route_action == "role_llm_session"
    assert not (pending_dir / "pending-stale-advance.json").exists()


@pytest.mark.asyncio
async def test_stale_advance_pending_cleaned_even_on_confirm_word(
    tmp_path: Path, llm_agent_mock, monkeypatch
):
    """Edge case: even if the user types 确认 while a stale
    advance_sprint_state pending sits on disk, we still skip
    executing that stale action (because its gate has been removed)
    and treat 确认 as a regular user message to the LLM. This avoids
    re-introducing the original crash path where a stale pending got
    "confirmed" long after its semantic window closed.
    """
    repo_root = _build_repo_fixture(tmp_path)
    monkeypatch.setattr(
        "feishu_agent.runtime.feishu_runtime_service.settings.app_repo_root",
        str(repo_root),
    )

    pending_dir = repo_root / "data" / "techbot-runs" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    action = PendingAction(
        trace_id="pending-stale-confirm",
        chat_id="chat-stale-confirm",
        role_name="tech_lead",
        action_type="advance_sprint_state",
        action_args={"story_key": "x", "to_status": "review"},
    )
    PendingActionService(pending_dir).save(action)

    result = await process_role_message(
        role_name="tech-lead-planner",
        command_text="确认",
        trace_id="trace-stale-confirm",
        chat_id="chat-stale-confirm",
        bot_context=_bot_context(),
        llm_agent_adapter=llm_agent_mock,
    )

    assert result.route_action == "role_llm_session"
    assert not (pending_dir / "pending-stale-confirm.json").exists()


@pytest.mark.asyncio
async def test_confirm_execution_failure_still_deletes_pending(tmp_path: Path, llm_agent_mock, monkeypatch):
    """F2 fix: if the underlying action raises, the pending file is still cleaned up."""
    repo_root = _build_repo_fixture(tmp_path)
    monkeypatch.setattr("feishu_agent.runtime.feishu_runtime_service.settings.app_repo_root", str(repo_root))

    pending_dir = repo_root / "data" / "techbot-runs" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    action = PendingAction(
        trace_id="pending-fail",
        chat_id="chat-fail",
        role_name="tech_lead",
        action_type="write_progress_sync",
        action_args={},
    )
    PendingActionService(pending_dir).save(action)

    def _exploding_service(ctx, **kw):
        mock = MagicMock()
        mock.execute = AsyncMock(side_effect=RuntimeError("Feishu API down"))
        return mock

    monkeypatch.setattr(
        "feishu_agent.runtime.feishu_runtime_service.build_progress_sync_service",
        _exploding_service,
    )

    result = await process_role_message(
        role_name="tech-lead-planner",
        command_text="确认",
        trace_id="trace-fail",
        chat_id="chat-fail",
        bot_context=_bot_context(),
        llm_agent_adapter=llm_agent_mock,
    )

    assert result.ok is False
    assert "失败" in result.message
    assert result.route_action == "error"
    assert not (pending_dir / "pending-fail.json").exists()


# ======================================================================
# T094e — force_sync_to_remote execute path
# ======================================================================


requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not available"
)


def _fs_git_env(home: Path) -> dict[str, str]:
    return {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
    }


def _fs_git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env=_fs_git_env(cwd),
    )


def _build_force_sync_fixture(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """Build a FeishuOPC-shaped repo_root + a real project git clone
    with diverged ``main``. Returns ``(app_repo_root, project_repo,
    local_div_sha, remote_tip_sha)``.
    """
    app_repo_root = tmp_path / "app"
    app_repo_root.mkdir()

    bare = tmp_path / "project-remote.git"
    _fs_git(tmp_path, "init", "--bare", str(bare))

    project_repo = tmp_path / "project-work"
    project_repo.mkdir()
    _fs_git(project_repo, "init", "-q", "-b", "main")
    (project_repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _fs_git(project_repo, "add", "seed.txt")
    _fs_git(project_repo, "commit", "-q", "-m", "seed")
    _fs_git(project_repo, "remote", "add", "origin", str(bare))
    _fs_git(project_repo, "push", "-q", "origin", "main")

    # Upstream diverges via a sibling clone.
    sib = tmp_path / "sibling"
    subprocess.run(
        ["git", "clone", "-q", str(bare), str(sib)],
        check=True,
        capture_output=True,
    )
    _fs_git(sib, "checkout", "-q", "main")
    (sib / "upstream.txt").write_text("u\n", encoding="utf-8")
    _fs_git(sib, "add", "upstream.txt")
    _fs_git(sib, "commit", "-q", "-m", "upstream only")
    _fs_git(sib, "push", "-q", "origin", "main")
    remote_tip = _fs_git(sib, "rev-parse", "HEAD").stdout.strip()

    (project_repo / "local.txt").write_text("l\n", encoding="utf-8")
    _fs_git(project_repo, "add", "local.txt")
    _fs_git(project_repo, "commit", "-q", "-m", "local only")
    local_div = _fs_git(project_repo, "rev-parse", "HEAD").stdout.strip()

    # App-level registry + policy wiring — matches the real on-disk
    # layout ``feishu_runtime_service`` reads.
    projects_dir = app_repo_root / ".larkagent" / "secrets" / "projects"
    projects_dir.mkdir(parents=True)
    (projects_dir / "projects.jsonl").write_text(
        json.dumps(
            {
                "project_id": "proj-force",
                "display_name": "ForceSync Test",
                "project_repo_root": str(project_repo),
                "is_default": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    policies_dir = app_repo_root / ".larkagent" / "secrets" / "code_write"
    policies_dir.mkdir(parents=True)
    (policies_dir / "policies.jsonl").write_text(
        json.dumps(
            {
                "project_id": "proj-force",
                "project_repo_root": str(project_repo),
                "allowed_write_roots": ["./"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    return app_repo_root, project_repo, local_div, remote_tip


@requires_git
@pytest.mark.asyncio
async def test_execute_force_sync_pending_resets_to_origin_main(
    tmp_path: Path,
):
    _reset_runtime_caches()
    app_repo_root, project_repo, local_div, remote_tip = (
        _build_force_sync_fixture(tmp_path)
    )
    registry = build_project_registry(app_repo_root=app_repo_root)
    assert "proj-force" in registry

    pending = PendingAction(
        trace_id="pending-fs-001",
        chat_id="chat-fs",
        role_name="tech_lead",
        action_type="force_sync_to_remote",
        action_args={
            "project_id": "proj-force",
            "remote": "origin",
            "target_branch": "main",
            "ahead": 1,
            "behind": 1,
            "current_branch": "main",
        },
    )

    result = await _execute_force_sync_pending(
        pending=pending,
        repo_root=app_repo_root,
        registry=registry,
        project_id="proj-force",
        trace="trace-fs-exec",
    )

    assert result.ok is True
    assert result.route_action == "pending_executed"
    assert "硬重置" in result.message
    assert remote_tip[:8] in result.message
    # The destructive pipeline must have actually moved HEAD.
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(project_repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head_after == remote_tip
    # Local divergent SHA must still be reachable via reflog for
    # recovery.
    reflog = subprocess.run(
        ["git", "reflog", "--pretty=%H"],
        cwd=str(project_repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert local_div in reflog


@requires_git
@pytest.mark.asyncio
async def test_execute_force_sync_pending_reports_git_error(
    tmp_path: Path,
):
    _reset_runtime_caches()
    app_repo_root, _project_repo, _div, _tip = _build_force_sync_fixture(
        tmp_path
    )
    registry = build_project_registry(app_repo_root=app_repo_root)

    pending = PendingAction(
        trace_id="pending-fs-err",
        chat_id="chat-fs-err",
        role_name="tech_lead",
        action_type="force_sync_to_remote",
        action_args={
            "project_id": "proj-force",
            "remote": "origin",
            "target_branch": "does-not-exist",
        },
    )

    result = await _execute_force_sync_pending(
        pending=pending,
        repo_root=app_repo_root,
        registry=registry,
        project_id="proj-force",
        trace="trace-fs-err",
    )

    assert result.ok is False
    assert result.route_action == "error"
    assert "硬重置失败" in result.message


@requires_git
@pytest.mark.asyncio
async def test_execute_force_sync_pending_unknown_project(
    tmp_path: Path,
):
    _reset_runtime_caches()
    app_repo_root, _project_repo, _div, _tip = _build_force_sync_fixture(
        tmp_path
    )
    registry = build_project_registry(app_repo_root=app_repo_root)

    pending = PendingAction(
        trace_id="pending-fs-no-id",
        chat_id="chat-fs-no-id",
        role_name="tech_lead",
        action_type="force_sync_to_remote",
        action_args={},
    )

    result = await _execute_force_sync_pending(
        pending=pending,
        repo_root=app_repo_root,
        registry=registry,
        project_id="",
        trace="trace-fs-no-id",
    )

    assert result.ok is False
    assert "project_id" in result.message


@requires_git
@pytest.mark.asyncio
async def test_execute_pending_action_dispatches_force_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Regression: _execute_pending_action used to reference an
    undefined ``repo_root`` when dispatching force_sync_to_remote,
    producing ``name 'repo_root' is not defined`` at runtime. Drive
    the dispatcher end-to-end so this failure would be caught before
    it reaches Feishu again.
    """
    _reset_runtime_caches()
    app_repo_root, project_repo, _div, remote_tip = _build_force_sync_fixture(
        tmp_path
    )
    from feishu_agent.runtime import feishu_runtime_service as runtime_mod
    monkeypatch.setattr(
        runtime_mod.settings, "app_repo_root", str(app_repo_root)
    )

    pending = PendingAction(
        trace_id="pending-fs-dispatch",
        chat_id="chat-fs-dispatch",
        role_name="tech_lead",
        action_type="force_sync_to_remote",
        action_args={
            "project_id": "proj-force",
            "remote": "origin",
            "target_branch": "main",
        },
    )

    result = await _execute_pending_action(
        pending=pending,
        context=_bot_context(),
        trace="trace-fs-dispatch",
    )

    assert result.ok is True, result.message
    assert "repo_root" not in result.message
    assert remote_tip[:8] in result.message
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(project_repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head_after == remote_tip
