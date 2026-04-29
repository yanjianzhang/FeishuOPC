from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from feishu_agent.core.llm_gateway_shim import (
    ExecutionResult,
    MockGateway as _BaseMockGateway,
    TokenUsage,
    ToolCall,
    ToolPolicy,
)

from feishu_agent.core.llm_agent_adapter import LlmAgentAdapter, LlmSessionResult


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


@pytest_asyncio.fixture()
async def adapter():
    mock = HttpOnlyMockGateway()
    mock.register("agents.create", lambda p: {"agentId": p.get("agentId", "test"), "status": "created"})
    mock.register("chat.send", lambda p: _build_chat_response("Hello from mock"))
    mock.register("config.get", lambda p: {"agentId": "test", "tools": {}})
    mock.register("config.set", lambda p: {"ok": True})
    mock.register("config.patch", lambda p: {"ok": True})
    await mock.connect()

    adp = LlmAgentAdapter(
        gateway_url="ws://mock:18789/gateway",
        default_model="doubao-seed-2-0-pro-260215",
        gateway=mock,
        timeout=30,
    )
    await adp.connect()
    return adp


@pytest.mark.asyncio
async def test_create_agent_returns_agent(adapter: LlmAgentAdapter):
    agent = await adapter.create_agent(
        agent_id="test-agent",
        system_prompt="You are a test agent.",
    )
    assert agent is not None
    assert hasattr(agent, "execute")


@pytest.mark.asyncio
async def test_execute_agent_returns_session_result(adapter: LlmAgentAdapter):
    agent = await adapter.create_agent(
        agent_id="exec-agent",
        system_prompt="Test",
    )
    result = await adapter.execute_agent(agent, "What is 2+2?")

    assert isinstance(result, LlmSessionResult)
    assert isinstance(result.content, str)
    assert isinstance(result.latency_ms, int)


@pytest.mark.asyncio
async def test_spawn_sub_agent(adapter: LlmAgentAdapter):
    result = await adapter.spawn_sub_agent(
        role_name="sprint_planner",
        task="Plan the next sprint",
        system_prompt="You are a sprint planner.",
        tools_allow=["read_sprint_status"],
        model="doubao-seed-2-0-pro-260215",
    )

    assert isinstance(result, LlmSessionResult)
    assert isinstance(result.content, str)


@pytest.mark.asyncio
async def test_create_agent_with_tool_policy(adapter: LlmAgentAdapter):
    agent = await adapter.create_agent(
        agent_id="policy-agent",
        system_prompt="Test",
        tool_policy=ToolPolicy(profile="minimal", allow=["read_sprint_status"]),
    )
    assert agent is not None


def test_from_execution_result_maps_all_fields():
    token_usage = TokenUsage(input=100, output=50, cache_read=10, cache_write=5, total_tokens=165)
    tc = ToolCall(tool="read_file", input='{"path": "foo.py"}', output="contents", duration_ms=42)

    result = ExecutionResult(
        success=True,
        content="Analysis complete",
        tool_calls=[tc],
        latency_ms=1234,
        token_usage=token_usage,
        stop_reason="end_turn",
    )

    mapped = LlmSessionResult.from_execution_result(result)

    assert mapped.success is True
    assert mapped.content == "Analysis complete"
    assert mapped.latency_ms == 1234
    assert mapped.stop_reason == "end_turn"
    assert mapped.error_message is None
    assert len(mapped.tool_calls) == 1
    assert mapped.tool_calls[0]["tool"] == "read_file"
    assert mapped.tool_calls[0]["duration_ms"] == 42
    assert mapped.token_usage["input"] == 100
    assert mapped.token_usage["output"] == 50
    assert mapped.token_usage["total_tokens"] == 165


def test_from_execution_result_handles_empty_fields():
    result = ExecutionResult(
        success=False,
        content="",
        latency_ms=0,
        error_message="Timeout",
    )

    mapped = LlmSessionResult.from_execution_result(result)

    assert mapped.success is False
    assert mapped.content == ""
    assert mapped.tool_calls == []
    assert mapped.token_usage["total_tokens"] >= 0
    assert mapped.error_message == "Timeout"


@pytest.mark.asyncio
async def test_execute_agent_respects_custom_timeout(adapter: LlmAgentAdapter):
    agent = await adapter.create_agent(
        agent_id="timeout-agent",
        system_prompt="Test",
    )
    result = await adapter.execute_agent(agent, "quick query", timeout_seconds=5)
    assert isinstance(result, LlmSessionResult)


@pytest.mark.asyncio
async def test_spawn_sub_agent_without_tools(adapter: LlmAgentAdapter):
    result = await adapter.spawn_sub_agent(
        role_name="reviewer",
        task="Review code",
        system_prompt="You are a reviewer.",
    )
    assert isinstance(result, LlmSessionResult)


@pytest.mark.asyncio
async def test_connect_required_before_use():
    adp = LlmAgentAdapter(
        gateway_url="ws://mock:18789/gateway",
        default_model="test-model",
    )

    with pytest.raises(RuntimeError, match="not connected"):
        await adp.create_agent("x", "prompt")
