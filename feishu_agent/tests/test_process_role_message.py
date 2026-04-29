from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import yaml
from feishu_agent.core.llm_gateway_shim import MockGateway as _BaseMockGateway

from feishu_agent.core.llm_agent_adapter import LlmAgentAdapter
from feishu_agent.runtime.feishu_runtime_service import (
    FeishuBotContext,
    process_role_message,
)


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


def _bot_context() -> FeishuBotContext:
    return FeishuBotContext(
        bot_name="tech_lead",
        app_id="app-test",
        app_secret="secret-test",
        verification_token=None,
        encrypt_key=None,
    )


def _pm_context() -> FeishuBotContext:
    return FeishuBotContext(
        bot_name="product_manager",
        app_id="pm-app",
        app_secret="pm-secret",
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
        "---\ntags: [plan]\ntool_allow_list: [read_sprint_status, advance_sprint_state]\n---\nYou plan sprints.",
        encoding="utf-8",
    )

    adapter_dir = repo_root / "project-adapters"
    adapter_dir.mkdir()
    import json
    (adapter_dir / "exampleapp-progress.json").write_text(
        json.dumps({"source_roots": {"status_file": "sprint-status.yaml"}}),
        encoding="utf-8",
    )

    status_path = repo_root / "sprint-status.yaml"
    status_path.write_text(
        yaml.safe_dump({"sprint_name": "Sprint 5", "current_sprint": {"goal": "Integration"}}, allow_unicode=True),
        encoding="utf-8",
    )

    log_dir = repo_root / "server" / "data" / "techbot-runs"
    log_dir.mkdir(parents=True)

    return repo_root


@pytest_asyncio.fixture()
async def llm_agent_mock():
    mock_gw = HttpOnlyMockGateway()
    mock_gw.register("agents.create", lambda p: {"agentId": p.get("agentId", "test"), "status": "created"})
    mock_gw.register("chat.send", lambda p: _build_chat_response("技术组长回复：Sprint 5 状态正常"))
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
async def test_tech_lead_role_llm_session(tmp_path: Path, llm_agent_mock, monkeypatch):
    repo_root = _build_repo_fixture(tmp_path)
    monkeypatch.setattr("feishu_agent.runtime.feishu_runtime_service.settings.app_repo_root", str(repo_root))

    result = await process_role_message(
        role_name="tech-lead-planner",
        command_text="查看当前 sprint 状态",
        trace_id="trace-test-001",
        chat_id="chat-001",
        bot_context=_bot_context(),
        llm_agent_adapter=llm_agent_mock,
    )
    assert result.ok is True
    assert result.trace_id == "trace-test-001"
    assert "Sprint 5" in result.message
    assert result.route_action == "role_llm_session"


@pytest.mark.asyncio
async def test_product_manager_role_llm_session(tmp_path: Path, llm_agent_mock, monkeypatch):
    repo_root = _build_repo_fixture(tmp_path)
    monkeypatch.setattr("feishu_agent.runtime.feishu_runtime_service.settings.app_repo_root", str(repo_root))

    result = await process_role_message(
        role_name="product-manager",
        command_text="帮我写 PRD",
        trace_id="trace-pm-001",
        chat_id="chat-pm",
        bot_context=_pm_context(),
        llm_agent_adapter=llm_agent_mock,
    )
    assert result.ok is True
    assert result.route_action == "role_llm_session"
    assert result.trace_id == "trace-pm-001"


@pytest.mark.asyncio
async def test_llm_agent_connection_failure(tmp_path: Path, monkeypatch):
    repo_root = _build_repo_fixture(tmp_path)
    monkeypatch.setattr("feishu_agent.runtime.feishu_runtime_service.settings.app_repo_root", str(repo_root))

    broken_adapter = LlmAgentAdapter(
        gateway_url="ws://127.0.0.1:1/gateway",
        default_model="test-model",
        timeout=2,
    )
    broken_adapter.connect = AsyncMock(side_effect=ConnectionRefusedError("mock connection refused"))

    result = await process_role_message(
        role_name="tech-lead-planner",
        command_text="测试连接失败",
        trace_id="trace-fail-001",
        chat_id="chat-fail",
        bot_context=_bot_context(),
        llm_agent_adapter=broken_adapter,
    )
    assert result.ok is False
    assert "失败" in result.message or "error" in result.route_action.lower()
