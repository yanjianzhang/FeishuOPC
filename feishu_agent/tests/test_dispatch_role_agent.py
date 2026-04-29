from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
import yaml
from feishu_agent.core.llm_gateway_shim import MockGateway as _BaseMockGateway

from feishu_agent.core.llm_agent_adapter import LlmAgentAdapter
from feishu_agent.roles.role_registry_service import RoleRegistryService
from feishu_agent.roles.tech_lead_executor import TechLeadToolExecutor
from feishu_agent.team.audit_service import AuditService
from feishu_agent.team.sprint_state_service import SprintStateService
from feishu_agent.tools.progress_sync_service import ProgressSyncService


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


def _write_role_file(roles_dir: Path, role_name: str, *, tags: list[str] | None = None,
                      tool_allow_list: list[str] | None = None, model: str | None = None,
                      body: str = "You are a test role agent.") -> None:
    frontmatter: dict[str, Any] = {}
    if tags:
        frontmatter["tags"] = tags
    if tool_allow_list:
        frontmatter["tool_allow_list"] = tool_allow_list
    if model:
        frontmatter["model"] = model
    fm_str = yaml.safe_dump(frontmatter, default_flow_style=True).strip() if frontmatter else ""
    content = f"---\n{fm_str}\n---\n{body}" if fm_str else body
    (roles_dir / f"{role_name}.md").write_text(content, encoding="utf-8")


@pytest_asyncio.fixture()
async def setup(tmp_path: Path):
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()
    _write_role_file(
        roles_dir, "sprint_planner",
        tags=["plan"], tool_allow_list=["read_sprint_status", "advance_sprint_state"],
        body="You are the Sprint Planner.",
    )
    _write_role_file(
        roles_dir, "repo_inspector",
        tags=["execute"], tool_allow_list=["read_sprint_status"],
        model="fast-model",
        body="You are the Repo Inspector.",
    )

    mock_gw = HttpOnlyMockGateway()
    mock_gw.register("agents.create", lambda p: {"agentId": p.get("agentId", "test"), "status": "created"})
    mock_gw.register("chat.send", lambda p: _build_chat_response("Sub-agent completed task"))
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
        yaml.safe_dump({"sprint_name": "Sprint 3", "current_sprint": {"goal": "Test"}}, allow_unicode=True),
        encoding="utf-8",
    )

    executor = TechLeadToolExecutor(
        progress_sync_service=MagicMock(spec=ProgressSyncService),
        sprint_state_service=SprintStateService(tmp_path, status_file),
        audit_service=AuditService(tmp_path / "audit"),
        llm_agent_adapter=adapter,
        role_registry=RoleRegistryService(roles_dir),
        project_id="test-project",
        command_text="test",
        trace_id="trace-dispatch",
        # 600s matches production's default ``role_agent_timeout_seconds``
        # closely enough that ``_remaining_seconds`` clears the
        # ``MIN_SUB_AGENT_TIMEOUT_SECONDS`` floor for every dispatch in
        # this fixture. Individual tests that need to probe the
        # out-of-budget path shrink this via a dedicated setup or
        # monkeypatch ``self.timeout_seconds`` directly.
        timeout_seconds=600,
    )
    return executor, mock_gw, roles_dir


@pytest.mark.asyncio
async def test_dispatch_success(setup):
    executor, _, _ = setup
    result = await executor.execute_tool("dispatch_role_agent", {
        "role_name": "sprint_planner",
        "task": "Plan the next sprint iteration",
    })
    assert result["success"] is True
    assert result["role_name"] == "sprint_planner"
    assert result["task"] == "Plan the next sprint iteration"
    assert isinstance(result["output"], str)
    assert result["error"] is None
    assert isinstance(result["latency_ms"], int)


@pytest.mark.asyncio
async def test_dispatch_unknown_role(setup):
    executor, _, _ = setup
    result = await executor.execute_tool("dispatch_role_agent", {
        "role_name": "nonexistent_agent",
        "task": "Do something",
    })
    assert result["success"] is False
    assert "UNKNOWN_ROLE" in result["error"]
    assert "nonexistent_agent" in result["error"]


@pytest.mark.asyncio
async def test_dispatch_timeout(setup, monkeypatch):
    executor, _, _ = setup

    async def _timeout_spawn(*args, **kwargs):
        raise TimeoutError("Agent timed out")

    monkeypatch.setattr(executor._llm_agent, "spawn_sub_agent", _timeout_spawn)

    result = await executor.execute_tool("dispatch_role_agent", {
        "role_name": "sprint_planner",
        "task": "Very long task",
    })
    assert result["success"] is False
    assert result["error"] == "AGENT_TIMEOUT"


@pytest.mark.asyncio
async def test_dispatch_generic_error(setup, monkeypatch):
    executor, _, _ = setup

    async def _error_spawn(*args, **kwargs):
        raise ConnectionError("Gateway unreachable")

    monkeypatch.setattr(executor._llm_agent, "spawn_sub_agent", _error_spawn)

    result = await executor.execute_tool("dispatch_role_agent", {
        "role_name": "sprint_planner",
        "task": "Do work",
    })
    assert result["success"] is False
    assert "Gateway unreachable" in result["error"]


@pytest.mark.asyncio
async def test_dispatch_passes_role_params(setup, monkeypatch):
    executor, _, _ = setup
    captured: dict[str, Any] = {}

    original_spawn = executor._llm_agent.spawn_sub_agent

    async def _capture_spawn(**kwargs):
        captured.update(kwargs)
        return await original_spawn(**kwargs)

    monkeypatch.setattr(executor._llm_agent, "spawn_sub_agent", lambda **kw: _capture_spawn(**kw))

    await executor.execute_tool("dispatch_role_agent", {
        "role_name": "repo_inspector",
        "task": "Inspect the repo",
        "acceptance_criteria": "Return file list",
    })

    assert captured["role_name"] == "repo_inspector"
    assert "You are the Repo Inspector." in captured["system_prompt"]
    assert "Return file list" in captured["system_prompt"]
    assert captured["tools_allow"] == ["read_sprint_status"]
    assert captured["model"] == "fast-model"


@pytest.mark.asyncio
async def test_dispatch_with_acceptance_criteria(setup):
    executor, _, _ = setup
    result = await executor.execute_tool("dispatch_role_agent", {
        "role_name": "sprint_planner",
        "task": "Plan sprint 4",
        "acceptance_criteria": "Must include risk assessment",
    })
    assert result["success"] is True


# ---------------------------------------------------------------------------
# Sub-agent minimum budget guard
#
# Pins the 2026-04-20 Story 3-2 review-cycle-2 incident: after the
# first reviewer cycle + bug_fixer consumed ~860s of the TL's 900s
# budget, the TL then dispatched reviewer 2 with ``_remaining_seconds()
# = max(900 - 860 - 30, 15) = 15s`` (bumped to 40s in the screenshot
# because of slightly different timing), which cannot fit even one
# LLM round-trip on the Anthropic relay. The new gate short-circuits
# this to an ``OUT_OF_BUDGET`` error before any LLM spend happens.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_refuses_when_remaining_budget_below_minimum(
    setup, monkeypatch
):
    """When the tech-lead's overall budget has less than
    ``MIN_SUB_AGENT_TIMEOUT_SECONDS`` seconds left, ``dispatch_role_agent``
    must refuse to spawn instead of handing the sub-agent an
    unworkable 15–40s window."""
    from feishu_agent.roles import tech_lead_executor as tle

    executor, _, _ = setup

    # Shrink the TL's overall budget so ``_remaining_seconds()`` falls
    # well under the floor, mimicking end-of-session conditions.
    executor.timeout_seconds = 60
    assert tle.MIN_SUB_AGENT_TIMEOUT_SECONDS > 60  # sanity: test is meaningful

    spawn_called = {"count": 0}

    async def _never_spawn(**_kwargs):
        spawn_called["count"] += 1
        return None

    monkeypatch.setattr(executor._llm_agent, "spawn_sub_agent", _never_spawn)
    monkeypatch.setattr(
        executor._llm_agent,
        "spawn_sub_agent_with_tools",
        _never_spawn,
    )

    result = await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "sprint_planner", "task": "Plan sprint 5"},
    )
    assert result["success"] is False
    assert result["error"] == "OUT_OF_BUDGET"
    # We must short-circuit BEFORE hitting the LLM — no LLM spend.
    assert spawn_called["count"] == 0
    # The guidance string must tell the LLM not to retry, otherwise
    # the tool loop will just call dispatch_role_agent again.
    assert "DO NOT retry" in result["message"]


@pytest.mark.asyncio
async def test_dispatch_proceeds_when_budget_meets_minimum(setup):
    """Counterpart to the refusal test: with a healthy budget (the
    default 600s fixture), dispatch proceeds normally. Guards against
    an over-eager guard that would regress the happy path."""
    from feishu_agent.roles import tech_lead_executor as tle

    executor, _, _ = setup
    # Fixture default is 600s; ensure the assumption that this clears
    # the floor by a wide margin — so a future bump to the MIN doesn't
    # silently turn every happy-path test into a refusal.
    assert executor.timeout_seconds > tle.MIN_SUB_AGENT_TIMEOUT_SECONDS + 60

    result = await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "sprint_planner", "task": "Plan sprint 5"},
    )
    assert result["success"] is True
    assert result.get("error") in (None, "")
