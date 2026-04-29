"""Tests for the MCP tool adapter.

We do NOT test the ``StdioMcpTransport`` end-to-end here — that would
require spawning a real subprocess and makes tests flaky. Instead we
validate:

- ``McpToolAdapter`` drives an injected transport correctly
  (handshake → tools/list → namespaced specs → tools/call routing).
- Namespacing prevents collisions.
- Error responses from the server map to structured tool errors
  instead of raising through the loop.
- Oversized responses get truncated to the byte budget.
- ``CompositeToolExecutor`` routes ``mcp__*`` names to the right
  adapter and falls through to the native executor otherwise.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.tools.mcp_tool_adapter import (
    CompositeToolExecutor,
    InMemoryMcpTransport,
    McpCallError,
    McpToolAdapter,
)


def _default_handler(tools: list[dict[str, Any]]):
    """Build an MCP server stub whose ``tools/list`` returns ``tools``
    and whose ``tools/call`` echoes the arguments back.

    Tests that need more specific behavior (errors, oversized
    responses) build their own handler.
    """
    async def handler(method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return {"protocolVersion": "2024-11-05"}
        if method == "tools/list":
            return {"tools": tools}
        if method == "tools/call":
            return {
                "content": [
                    {"type": "text", "text": json.dumps(params["arguments"])}
                ]
            }
        raise McpCallError(-32601, f"Method not found: {method}")

    return handler


@pytest.mark.asyncio
async def test_adapter_connect_fetches_tools_and_namespaces():
    handler = _default_handler(
        [
            {"name": "search", "description": "search things"},
            {
                "name": "fetch",
                "description": "fetch a thing",
                "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
            },
        ]
    )
    transport = InMemoryMcpTransport(handler)
    adapter = McpToolAdapter(server_name="notes", transport=transport)

    await adapter.connect()

    specs = adapter.tool_specs()
    assert {s.name for s in specs} == {"mcp__notes__search", "mcp__notes__fetch"}
    # Default schema supplied when server omits one.
    search_spec = next(s for s in specs if s.name == "mcp__notes__search")
    assert search_spec.input_schema == {"type": "object", "properties": {}}

    await adapter.close()


@pytest.mark.asyncio
async def test_adapter_execute_tool_routes_to_remote():
    transport = InMemoryMcpTransport(
        _default_handler([{"name": "echo", "description": ""}])
    )
    adapter = McpToolAdapter(server_name="srv", transport=transport)
    await adapter.connect()

    result = await adapter.execute_tool(
        "mcp__srv__echo", {"message": "hello"}
    )
    # The stub returns {"content": [...]}; ``execute_tool`` passes it through
    # unchanged when under budget.
    assert isinstance(result, dict)
    assert "content" in result

    # One of the recorded calls should have been tools/call with the
    # de-namespaced remote name.
    tool_calls = [
        (m, p) for m, p in transport.calls if m == "tools/call"
    ]
    assert tool_calls == [("tools/call", {"name": "echo", "arguments": {"message": "hello"}})]
    await adapter.close()


@pytest.mark.asyncio
async def test_adapter_execute_unknown_tool_returns_structured_error():
    transport = InMemoryMcpTransport(_default_handler([]))
    adapter = McpToolAdapter(server_name="srv", transport=transport)
    await adapter.connect()

    result = await adapter.execute_tool("mcp__srv__missing", {})
    assert isinstance(result, dict)
    assert result["error"].startswith("UNKNOWN_MCP_TOOL")
    await adapter.close()


@pytest.mark.asyncio
async def test_adapter_server_error_becomes_tool_error():
    async def handler(method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return {}
        if method == "tools/list":
            return {"tools": [{"name": "broken"}]}
        if method == "tools/call":
            raise McpCallError(-32000, "boom", data={"hint": "check env"})
        return {}

    transport = InMemoryMcpTransport(handler)
    adapter = McpToolAdapter(server_name="srv", transport=transport)
    await adapter.connect()

    result = await adapter.execute_tool("mcp__srv__broken", {})
    assert result == {
        "error": "MCP_TOOL_ERROR",
        "server": "srv",
        "tool": "broken",
        "code": -32000,
        "message": "boom",
        "data": {"hint": "check env"},
    }
    await adapter.close()


@pytest.mark.asyncio
async def test_adapter_truncates_oversized_responses():
    big_payload = {"blob": "x" * 300_000}

    async def handler(method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return {}
        if method == "tools/list":
            return {"tools": [{"name": "big"}]}
        if method == "tools/call":
            return big_payload
        return {}

    transport = InMemoryMcpTransport(handler)
    adapter = McpToolAdapter(
        server_name="srv",
        transport=transport,
        response_byte_budget=1024,  # tiny budget for the test
    )
    await adapter.connect()

    result = await adapter.execute_tool("mcp__srv__big", {})
    assert isinstance(result, dict)
    assert result["error"] == "MCP_RESPONSE_TOO_LARGE"
    assert "sample" in result
    assert len(result["sample"]) <= 1024
    await adapter.close()


@pytest.mark.asyncio
async def test_composite_routes_native_and_mcp():
    """The composite executor should route ``mcp__*`` names to the
    right adapter and everything else to the native executor."""

    class NativeExec:
        def tool_specs(self):
            return [
                AgentToolSpec(
                    name="native_ping",
                    description="native ping",
                    input_schema={"type": "object"},
                )
            ]

        async def execute_tool(self, tool_name: str, arguments):
            return {"ok": True, "routed_to": "native"}

    handler = _default_handler([{"name": "ping", "description": ""}])
    transport = InMemoryMcpTransport(handler)
    mcp = McpToolAdapter(server_name="foo", transport=transport)
    await mcp.connect()

    composite = CompositeToolExecutor(native=NativeExec(), mcp_adapters=[mcp])

    # Union of specs.
    names = {s.name for s in composite.tool_specs()}
    assert names == {"native_ping", "mcp__foo__ping"}

    # Native route.
    res = await composite.execute_tool("native_ping", {})
    assert res == {"ok": True, "routed_to": "native"}

    # MCP route: server echoes the arguments.
    res = await composite.execute_tool("mcp__foo__ping", {"x": 1})
    assert isinstance(res, dict) and "content" in res

    await mcp.close()


@pytest.mark.asyncio
async def test_composite_rejects_malformed_mcp_name():
    class NativeExec:
        def tool_specs(self):
            return []

        async def execute_tool(self, tool_name, arguments):
            return {"ok": True}

    composite = CompositeToolExecutor(native=NativeExec(), mcp_adapters=[])
    bad = await composite.execute_tool("mcp__nowhere", {})
    assert bad == {"error": "MALFORMED_MCP_TOOL_NAME: mcp__nowhere"}


@pytest.mark.asyncio
async def test_composite_rejects_unknown_mcp_server():
    class NativeExec:
        def tool_specs(self):
            return []

        async def execute_tool(self, tool_name, arguments):
            return {"ok": True}

    composite = CompositeToolExecutor(native=NativeExec(), mcp_adapters=[])
    unknown = await composite.execute_tool("mcp__nope__call", {})
    assert unknown["error"] == "UNKNOWN_MCP_SERVER: nope"


def test_composite_refuses_duplicate_server_names():
    async def noop(method, params):
        return {}

    t1 = InMemoryMcpTransport(noop)
    t2 = InMemoryMcpTransport(noop)
    a1 = McpToolAdapter(server_name="dup", transport=t1)
    a2 = McpToolAdapter(server_name="dup", transport=t2)

    class NativeExec:
        def tool_specs(self):
            return []

        async def execute_tool(self, tool_name, arguments):
            return {"ok": True}

    with pytest.raises(ValueError, match="Duplicate MCP server"):
        CompositeToolExecutor(native=NativeExec(), mcp_adapters=[a1, a2])
