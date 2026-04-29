"""Minimal stand-in for the former ``openclaw_sdk`` mock gateway + client.

Production :class:`feishu_agent.core.llm_agent_adapter.LlmAgentAdapter` uses
direct HTTP to OpenAI-compatible ``/chat/completions``.  Unit tests inject a
``gateway=`` instance; this module provides the small surface those tests
registered handlers against — no external ``openclaw-sdk`` package.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ClientConfig:
    mode: str = "auto"
    timeout: int = 120


@dataclass
class ToolPolicy:
    profile: str = ""
    allow: list[str] | None = None
    deny: list[str] | None = None


@dataclass
class AgentConfig:
    agent_id: str
    system_prompt: str
    llm_provider: str = "openai"
    llm_model: str | None = None
    tool_policy: ToolPolicy | None = None


@dataclass
class ExecutionOptions:
    timeout_seconds: int = 120


@dataclass
class TokenUsage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    total_tokens: int = 0


@dataclass
class ToolCall:
    tool: str
    input: str
    output: str
    duration_ms: int = 0


@dataclass
class ExecutionResult:
    success: bool
    content: str = ""
    tool_calls: list[ToolCall] | None = None
    latency_ms: int = 0
    token_usage: TokenUsage | None = field(default_factory=TokenUsage)
    stop_reason: str | None = None
    error_message: str | None = None
    completed_at: Any | None = None


class MockGateway:
    """In-process RPC table keyed by logical method name (``agents.create`` …)."""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}

    def register(self, method: str, handler: Callable[[dict[str, Any]], Any]) -> None:
        self._handlers[method] = handler

    async def connect(self) -> None:
        return None

    async def subscribe(self, event_types: Any = None) -> None:
        """Override in tests to force the HTTP-only adapter path."""

    async def invoke(self, method: str, payload: dict[str, Any]) -> Any:
        fn = self._handlers.get(method)
        if fn is None:
            raise KeyError(f"MockGateway: no handler registered for {method!r}")
        result = fn(payload)
        if inspect.isawaitable(result):
            return await result  # type: ignore[misc]
        return result

    async def _maybe_call(self, method: str, payload: dict[str, Any]) -> Any:
        if method not in self._handlers:
            return None
        return await self.invoke(method, payload)


def _execution_result_from_chat_payload(raw: dict[str, Any]) -> ExecutionResult:
    usage = raw.get("usage") or {}
    tu = TokenUsage(
        input=int(usage.get("input_tokens", 0) or 0),
        output=int(usage.get("output_tokens", 0) or 0),
        cache_read=int(usage.get("cache_read_tokens", 0) or 0),
        cache_write=int(usage.get("cache_write_tokens", 0) or 0),
        total_tokens=int(usage.get("total_tokens", 0) or 0),
    )
    status = raw.get("status")
    success = raw.get("success")
    if success is None:
        success = status in (None, "completed", "complete", "ok", True)
    stop = raw.get("stop_reason") or raw.get("finish_reason") or "end_turn"
    return ExecutionResult(
        success=bool(success),
        content=str(raw.get("content", "") or ""),
        tool_calls=[],
        latency_ms=int(raw.get("latency_ms", 0) or 0),
        token_usage=tu,
        stop_reason=str(stop),
        error_message=raw.get("error_message"),
    )


class _GatewayBoundAgent:
    __slots__ = ("_gw", "agent_id", "system_prompt", "model")

    def __init__(
        self, gw: MockGateway, agent_id: str, system_prompt: str, model: str
    ) -> None:
        self._gw = gw
        self.agent_id = agent_id
        self.system_prompt = system_prompt
        self.model = model

    async def execute(self, query: str, *, options: ExecutionOptions | None = None) -> ExecutionResult:
        t0 = time.monotonic()
        payload: dict[str, Any] = {
            "agentId": self.agent_id,
            "query": query,
            "message": query,
        }
        if options is not None:
            payload["timeout"] = getattr(options, "timeout_seconds", None)
        raw = await self._gw.invoke("chat.send", payload)
        if not isinstance(raw, dict):
            raw = {"content": str(raw), "status": "completed"}
        result = _execution_result_from_chat_payload(raw)
        if not result.latency_ms:
            result.latency_ms = int((time.monotonic() - t0) * 1000)
        return result


class OpenClawClient:
    """Tiny client: ``create_agent`` → bound agent with ``execute`` → ``chat.send``."""

    def __init__(self, *, config: ClientConfig, gateway: MockGateway) -> None:
        self._config = config
        self._gateway = gateway

    async def create_agent(self, agent_config: AgentConfig) -> _GatewayBoundAgent:
        await self._gateway._maybe_call(
            "config.get", {"agentId": agent_config.agent_id}
        )
        if agent_config.tool_policy is not None:
            await self._gateway._maybe_call(
                "config.set",
                {
                    "agentId": agent_config.agent_id,
                    "toolPolicy": agent_config.tool_policy,
                },
            )
        create_payload = {
            "agentId": agent_config.agent_id,
            "systemPrompt": agent_config.system_prompt,
            "llmModel": agent_config.llm_model or "",
            "llmProvider": agent_config.llm_provider,
        }
        raw = await self._gateway.invoke("agents.create", create_payload)
        agent_id = str((raw or {}).get("agentId") or agent_config.agent_id)
        await self._gateway._maybe_call(
            "config.patch", {"agentId": agent_id, "tools": {}}
        )
        model = agent_config.llm_model or ""
        return _GatewayBoundAgent(self._gateway, agent_id, agent_config.system_prompt, model)

    async def close(self) -> None:
        return None


__all__ = [
    "AgentConfig",
    "ClientConfig",
    "ExecutionOptions",
    "ExecutionResult",
    "MockGateway",
    "OpenClawClient",
    "TokenUsage",
    "ToolCall",
    "ToolPolicy",
]
