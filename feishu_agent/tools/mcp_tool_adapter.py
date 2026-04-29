"""Model Context Protocol (MCP) adapter for FeishuOPC agents.

Context
-------
Hermes Agent v2026.x treats MCP servers as a first-class tool source:
you list a server in config, and every tool the server advertises via
``tools/list`` becomes available to the agent. We have lark-cli and a
bunch of other MCP servers already configured for Cursor — we'd like
FeishuOPC runtime to consume the same servers without hand-wrapping
each tool (``bitable_add_record`` etc.).

This file gives us:

1. **``McpTransport`` protocol** — the wire interface. Real transports
   (stdio, HTTP-SSE) plug in; tests use ``InMemoryMcpTransport``.
2. **``StdioMcpTransport``** — launches an MCP server as a subprocess
   and speaks JSON-RPC over its stdin/stdout. The common case (all
   ``lark-*`` skills use this).
3. **``McpToolAdapter``** — implements ``AgentToolExecutor`` by
   namespacing each remote tool as ``mcp__<server>__<tool>`` and
   translating ``execute_tool`` into ``tools/call``.
4. **``CompositeToolExecutor``** — merges native + N MCP executors
   into one ``AgentToolExecutor`` the adapter already knows how to
   drive. Keeps the LLM tool loop blissfully unaware that some
   tools live in a subprocess.

Security posture
----------------
MCP tools land behind the same ``AllowListedToolExecutor`` layer we
already use — an agent only sees the MCP tool names its role
explicitly opts into. We also apply a byte-budget cap on responses
so a runaway MCP server can't OOM the tool loop.

Non-goals (for now)
-------------------
- Streaming tool results (we buffer the full response).
- Capability negotiation beyond the core handshake.
- MCP resources / prompts — only ``tools`` is wired. Resources and
  prompts can come later; the transport handles generic requests.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from feishu_agent.core.agent_types import AgentToolExecutor, AgentToolSpec

logger = logging.getLogger(__name__)

# Tool names are namespaced ``mcp__<server>__<tool>`` for two reasons:
# 1. It prevents name collisions between two MCP servers that ship a
#    tool with the same name.
# 2. The ``mcp__`` prefix makes it trivially obvious in audit logs
#    which calls went out to an external process vs. a native tool.
MCP_TOOL_PREFIX = "mcp__"
MCP_TOOL_SEP = "__"

# Maximum bytes an MCP tool may return before we truncate. Chosen at
# 256 KiB because a pathological tool that dumps a whole file could
# otherwise blow through the LLM context and our audit log at the same
# time. Truncation is lossy; the caller sees a structured error hint.
DEFAULT_RESPONSE_BYTE_BUDGET = 256 * 1024


# =============================================================================
# Transport protocol
# =============================================================================


class McpProtocolError(Exception):
    """Raised when the MCP wire protocol is violated.

    Includes parse errors, missing ``jsonrpc`` field, mismatched
    response IDs. Differentiated from ``McpCallError`` (which is a
    well-formed error response from the server) so callers can
    distinguish "our client/transport is buggy" from "the remote
    tool failed."
    """


class McpCallError(Exception):
    """Raised when an MCP server returns a ``{"error": {...}}`` response.

    Carries the ``code`` and ``data`` fields so structured error
    handling is possible; ``message`` comes straight from the server.
    """

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"MCP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class McpTransport(Protocol):
    """Minimal protocol every MCP transport must satisfy.

    The adapter only ever calls ``send_request`` / ``send_notification``
    / ``start`` / ``stop``. It deliberately does NOT poke at stdin /
    subprocess internals — that keeps the transport pluggable for
    tests and future HTTP-SSE work.
    """

    async def start(self) -> None:
        """Bring the transport online. Idempotent."""

    async def stop(self) -> None:
        """Release transport resources. Idempotent."""

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and wait for the matching response.

        Returns the ``result`` object on success; raises
        ``McpCallError`` for well-formed error responses and
        ``McpProtocolError`` for anything else.
        """

    async def send_notification(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """Send a one-way JSON-RPC notification (no response awaited)."""


# =============================================================================
# Stdio transport (production — launches MCP server as subprocess)
# =============================================================================


@dataclass
class StdioMcpTransport:
    """Launch an MCP server as a subprocess and speak JSON-RPC over its
    stdio streams.

    Threading model
    ---------------
    One dedicated reader task consumes ``stdout`` line-by-line and
    dispatches responses to the correct waiting request via
    ``_pending``. Writes happen directly on ``send_request`` — JSON-RPC
    requests are cheap and bounded, so contention on ``stdin`` is
    acceptable. (We could queue writes too, but the complexity isn't
    worth it for our throughput.)

    Resilience
    ----------
    Reader-task death is visible through pending requests timing out:
    we intentionally don't try to respawn the subprocess. The adapter
    treats a dead transport as a permanent tool failure; the user
    fixes the MCP server config and restarts FeishuOPC.
    """

    command: list[str]
    env: dict[str, str] | None = None
    cwd: str | None = None
    name: str = "stdio-mcp"

    _proc: asyncio.subprocess.Process | None = field(default=None, init=False, repr=False)
    _reader_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _pending: dict[int, asyncio.Future[Any]] = field(
        default_factory=dict, init=False, repr=False
    )
    _id_counter: itertools.count = field(
        default_factory=lambda: itertools.count(1), init=False, repr=False
    )
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)
    _stderr_task: asyncio.Task[None] | None = field(
        default=None, init=False, repr=False
    )

    async def start(self) -> None:
        if self._started:
            return
        if not self.command:
            raise ValueError("StdioMcpTransport: command must be non-empty")
        self._proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
            cwd=self.cwd,
        )
        # Drain stderr into the logger so crashes / warnings are visible.
        # Without this the server's stderr fills its pipe buffer and
        # eventually blocks writes on its end.
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name=f"mcp-{self.name}-stderr"
        )
        self._reader_task = asyncio.create_task(
            self._read_responses(), name=f"mcp-{self.name}-reader"
        )
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._proc is not None:
            if self._proc.returncode is None:
                try:
                    self._proc.terminate()
                    await asyncio.wait_for(self._proc.wait(), timeout=2.0)
                except (asyncio.TimeoutError, ProcessLookupError):
                    try:
                        self._proc.kill()
                    except ProcessLookupError:
                        pass
        # Fail any still-pending requests so their awaiters don't hang.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(
                    McpProtocolError(f"MCP transport {self.name} stopped")
                )
        self._pending.clear()

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if not self._started or self._proc is None or self._proc.stdin is None:
            raise McpProtocolError(
                f"MCP transport {self.name} not started"
            )
        req_id = next(self._id_counter)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        # Serialize writes to avoid interleaving partial lines when
        # two coroutines race (protocol requires one-line JSON-RPC).
        async with self._lock:
            self._proc.stdin.write(line)
            await self._proc.stdin.drain()
        try:
            if timeout_seconds is not None:
                return await asyncio.wait_for(future, timeout=timeout_seconds)
            return await future
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise McpProtocolError(
                f"MCP request {method} timed out after {timeout_seconds}s"
            ) from None

    async def send_notification(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        if not self._started or self._proc is None or self._proc.stdin is None:
            raise McpProtocolError(
                f"MCP transport {self.name} not started"
            )
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._lock:
            self._proc.stdin.write(line)
            await self._proc.stdin.drain()

    # --- internal --------------------------------------------------------

    async def _read_responses(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stream = self._proc.stdout
        while True:
            line = await stream.readline()
            if not line:
                # EOF — server crashed or exited. Mark the transport
                # dead and let pending requests surface it.
                logger.warning("MCP transport %s: stdout closed", self.name)
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(
                            McpProtocolError(
                                f"MCP transport {self.name} exited unexpectedly"
                            )
                        )
                self._pending.clear()
                return
            try:
                obj = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                logger.warning(
                    "MCP transport %s: invalid JSON on stdout: %r",
                    self.name,
                    line[:200],
                )
                continue
            self._dispatch_incoming(obj)

    def _dispatch_incoming(self, obj: dict[str, Any]) -> None:
        # Notifications (``id`` missing) are silently ignored today.
        # When we add resource change subscriptions we'll handle them
        # here — see Hermes's mcp_tool.py for the shape.
        if "id" not in obj:
            return
        req_id = obj.get("id")
        if not isinstance(req_id, int):
            # Spec allows string ids; we only send int ids, so a non-int
            # id means the server echoed garbage or mixed with a
            # notification. Log and drop.
            logger.warning(
                "MCP transport %s: non-int response id=%r", self.name, req_id
            )
            return
        future = self._pending.pop(req_id, None)
        if future is None:
            logger.warning(
                "MCP transport %s: no awaiter for response id=%d",
                self.name,
                req_id,
            )
            return
        if "error" in obj:
            err = obj["error"] or {}
            future.set_exception(
                McpCallError(
                    code=int(err.get("code", -1)),
                    message=str(err.get("message", "unknown MCP error")),
                    data=err.get("data"),
                )
            )
        elif "result" in obj:
            future.set_result(obj["result"])
        else:
            future.set_exception(
                McpProtocolError(
                    "MCP response missing both 'result' and 'error'"
                )
            )

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                return
            logger.info(
                "mcp[%s][stderr] %s",
                self.name,
                line.decode("utf-8", errors="replace").rstrip(),
            )


# =============================================================================
# In-memory transport (tests only)
# =============================================================================


class InMemoryMcpTransport:
    """Test double that answers MCP requests from a handler callback.

    ``handler`` takes ``(method, params)`` and returns the JSON-RPC
    ``result`` payload, or raises ``McpCallError`` to simulate a
    server-side failure. Lets us exercise ``McpToolAdapter`` without
    spinning up a real subprocess.

    Also records every call in ``calls`` so tests can assert on
    dispatch shape (which method ran, what params were passed).
    """

    def __init__(
        self,
        handler: Callable[[str, dict[str, Any]], Awaitable[Any] | Any],
    ) -> None:
        self._handler = handler
        self._started = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    async def send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if not self._started:
            raise McpProtocolError("InMemoryMcpTransport not started")
        self.calls.append((method, dict(params or {})))
        maybe = self._handler(method, dict(params or {}))
        if asyncio.iscoroutine(maybe):
            return await maybe
        return maybe  # type: ignore[return-value]

    async def send_notification(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        if not self._started:
            raise McpProtocolError("InMemoryMcpTransport not started")
        self.calls.append((method, dict(params or {})))


# =============================================================================
# Adapter — presents one MCP server as an AgentToolExecutor
# =============================================================================


@dataclass
class McpToolAdapter:
    """Expose one MCP server's tools through the ``AgentToolExecutor``
    interface.

    Lifecycle:
        transport = StdioMcpTransport(command=["npx", "some-mcp"])
        adapter = McpToolAdapter(server_name="notes", transport=transport)
        await adapter.connect()    # handshake + tools/list
        # ...use as tool executor...
        await adapter.close()

    Tool naming: remote ``search`` becomes ``mcp__notes__search`` so
    multiple MCP servers can coexist without collisions.

    Refresh: call ``refresh_tools()`` to re-fetch the tool list. The
    MCP spec supports a ``notifications/tools/list_changed`` event
    but we don't subscribe yet — manual refresh covers the 90% case.
    """

    server_name: str
    transport: McpTransport
    call_timeout_seconds: float = 30.0
    response_byte_budget: int = DEFAULT_RESPONSE_BYTE_BUDGET

    _specs: list[AgentToolSpec] = field(default_factory=list, init=False, repr=False)
    _remote_names: dict[str, str] = field(
        default_factory=dict, init=False, repr=False
    )  # namespaced name -> remote name
    _connected: bool = field(default=False, init=False, repr=False)

    @staticmethod
    def namespace_tool(server_name: str, tool_name: str) -> str:
        return f"{MCP_TOOL_PREFIX}{server_name}{MCP_TOOL_SEP}{tool_name}"

    async def connect(self) -> None:
        """Start the transport, run the MCP init handshake, and cache
        the tool list. Must be called before using the adapter.

        We do the handshake every connect — we don't cache across
        process restarts because MCP server versions change and tool
        schemas shift.
        """
        if self._connected:
            return
        await self.transport.start()
        # Minimum-viable handshake. Real clients declare capabilities,
        # client info, and protocol version; we do the minimum the
        # spec requires to move on. If a server is stricter we'll
        # extend here.
        try:
            await self.transport.send_request(
                "initialize",
                params={
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "clientInfo": {
                        "name": "feishuopc",
                        "version": "tier2",
                    },
                },
                timeout_seconds=self.call_timeout_seconds,
            )
        except McpCallError as exc:
            await self.transport.stop()
            raise McpProtocolError(
                f"MCP server {self.server_name} rejected initialize: {exc}"
            ) from exc
        # Per spec, send the ``notifications/initialized`` note once
        # initialize succeeds so the server can flip into "ready"
        # state.
        try:
            await self.transport.send_notification("notifications/initialized")
        except Exception:
            logger.debug(
                "MCP server %s: notifications/initialized failed (non-fatal)",
                self.server_name,
                exc_info=True,
            )
        await self.refresh_tools()
        self._connected = True

    async def close(self) -> None:
        self._connected = False
        self._specs = []
        self._remote_names = {}
        await self.transport.stop()

    async def refresh_tools(self) -> list[AgentToolSpec]:
        """Fetch the current ``tools/list`` and rebuild local specs."""
        result = await self.transport.send_request(
            "tools/list", params={}, timeout_seconds=self.call_timeout_seconds
        )
        tools_raw = result.get("tools", [])
        specs: list[AgentToolSpec] = []
        mapping: dict[str, str] = {}
        for raw in tools_raw:
            remote_name = raw.get("name")
            if not remote_name or not isinstance(remote_name, str):
                logger.warning(
                    "MCP server %s: skipping tool with invalid name %r",
                    self.server_name,
                    raw,
                )
                continue
            namespaced = self.namespace_tool(self.server_name, remote_name)
            input_schema = raw.get("inputSchema") or {
                "type": "object",
                "properties": {},
            }
            specs.append(
                AgentToolSpec(
                    name=namespaced,
                    description=raw.get("description", "") or "",
                    input_schema=input_schema,
                )
            )
            mapping[namespaced] = remote_name
        self._specs = specs
        self._remote_names = mapping
        return list(specs)

    # --- AgentToolExecutor protocol --------------------------------------

    def tool_specs(self) -> list[AgentToolSpec]:
        return list(self._specs)

    async def execute_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | list[Any] | str:
        remote = self._remote_names.get(tool_name)
        if remote is None:
            return {
                "error": f"UNKNOWN_MCP_TOOL: {tool_name}",
                "server": self.server_name,
                "known": sorted(self._remote_names.keys()),
            }
        try:
            result = await self.transport.send_request(
                "tools/call",
                params={"name": remote, "arguments": arguments},
                timeout_seconds=self.call_timeout_seconds,
            )
        except McpCallError as exc:
            return {
                "error": "MCP_TOOL_ERROR",
                "server": self.server_name,
                "tool": remote,
                "code": exc.code,
                "message": exc.message,
                "data": exc.data,
            }
        except McpProtocolError as exc:
            return {
                "error": "MCP_PROTOCOL_ERROR",
                "server": self.server_name,
                "tool": remote,
                "message": str(exc),
            }
        return self._enforce_budget(result)

    def _enforce_budget(self, result: Any) -> Any:
        """Truncate pathologically large MCP responses.

        We serialize the result, measure UTF-8 bytes, and if it's
        over budget we return a structured error with a sample of
        the first N chars. This keeps a runaway tool from melting
        the LLM's context window and our audit logs.
        """
        try:
            serialized = json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            # Unserializable result — bubble up as a structured error
            # the LLM can see, rather than raising through the tool
            # loop.
            return {
                "error": "MCP_UNSERIALIZABLE_RESULT",
                "server": self.server_name,
                "type": type(result).__name__,
            }
        if len(serialized.encode("utf-8")) <= self.response_byte_budget:
            return result
        sample = serialized[:1024]
        return {
            "error": "MCP_RESPONSE_TOO_LARGE",
            "server": self.server_name,
            "byte_budget": self.response_byte_budget,
            "sample": sample,
        }


# =============================================================================
# Composite — merges native + multiple MCP adapters into one executor
# =============================================================================


class CompositeToolExecutor:
    """Merge a native ``AgentToolExecutor`` with zero or more MCP
    adapters into a single executor.

    The adapter's tool loop only knows how to drive one executor; we
    cheat by putting a dispatcher in front that routes ``execute_tool``
    by tool name:

    - Names starting with ``mcp__<server>__`` route to the adapter
      whose ``server_name`` matches ``<server>``.
    - Everything else falls through to the ``native`` executor.

    Tool specs are the union of all members. Collisions between
    native names and MCP names can't happen (namespace prefix), but
    two MCP servers registering with the same name WOULD collide —
    guard in ``__init__``.
    """

    def __init__(
        self,
        native: AgentToolExecutor,
        mcp_adapters: list[McpToolAdapter] | None = None,
    ) -> None:
        self._native = native
        self._adapters: dict[str, McpToolAdapter] = {}
        for adapter in mcp_adapters or []:
            if adapter.server_name in self._adapters:
                raise ValueError(
                    f"Duplicate MCP server name: {adapter.server_name}"
                )
            self._adapters[adapter.server_name] = adapter

    def tool_specs(self) -> list[AgentToolSpec]:
        specs = list(self._native.tool_specs())
        for adapter in self._adapters.values():
            specs.extend(adapter.tool_specs())
        return specs

    async def execute_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | list[Any] | str:
        if tool_name.startswith(MCP_TOOL_PREFIX):
            # Parse out the server name. Format: mcp__<server>__<tool>.
            stripped = tool_name[len(MCP_TOOL_PREFIX) :]
            parts = stripped.split(MCP_TOOL_SEP, 1)
            if len(parts) != 2:
                return {
                    "error": f"MALFORMED_MCP_TOOL_NAME: {tool_name}",
                }
            server = parts[0]
            adapter = self._adapters.get(server)
            if adapter is None:
                return {
                    "error": f"UNKNOWN_MCP_SERVER: {server}",
                    "known": sorted(self._adapters.keys()),
                }
            return await adapter.execute_tool(tool_name, arguments)
        return await self._native.execute_tool(tool_name, arguments)
