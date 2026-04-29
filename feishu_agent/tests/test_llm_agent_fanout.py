"""B-2 integration test for effect-aware fan-out.

Drives a minimal tool loop by stubbing both the LLM HTTP call
(``_send_chat_completion``) and the tool executor, then asserts that
two concurrent tool calls in the same turn run in parallel. The
walltime assertion uses a generous headroom (``< 1.5× of one call``)
so we're not flaky on slow CI — the key signal is "strictly less
than sequential", not a tight ratio.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import pytest

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.core.llm_agent_adapter import (
    AgentHandle,
    LlmAgentAdapter,
    LlmSessionResult,
)


class _SleepyExecutor:
    """Executor where every tool takes ``delay`` seconds. The tools
    are declared ``effect='read'`` so the partitioner groups them
    concurrent; a second ``world_tool`` spec lets us mix modes.
    """

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.call_order: list[tuple[str, float]] = []

    def tool_specs(self) -> list[AgentToolSpec]:
        return [
            AgentToolSpec(
                name="read_a",
                description="",
                input_schema={"type": "object"},
                effect="read",
            ),
            AgentToolSpec(
                name="read_b",
                description="",
                input_schema={"type": "object"},
                effect="read",
            ),
            AgentToolSpec(
                name="world_w",
                description="",
                input_schema={"type": "object"},
                effect="world",
            ),
        ]

    async def execute_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        t0 = time.monotonic()
        await asyncio.sleep(self.delay)
        self.call_order.append((tool_name, time.monotonic() - t0))
        return {"ok": True, "tool": tool_name}


def _tc(name: str, cid: str) -> dict[str, Any]:
    return {
        "id": cid,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def _chat_completion_with_tools(tool_calls: list[dict[str, Any]]) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": tool_calls,
                },
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
    }


def _chat_completion_final(text: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": text},
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
    }


async def _make_adapter(
    *, max_parallel: int | None
) -> LlmAgentAdapter:
    adapter = LlmAgentAdapter(
        llm_base_url="http://stub.invalid",
        llm_api_key="k",
        default_model="stub",
        timeout=30,
        max_parallel_tool_calls=max_parallel,
    )
    # Side-step connect() — the only thing _run_tool_loop needs on the
    # adapter is ``self._http`` to be non-None (gate check in
    # execute_with_tools). Provide a trivial placeholder client; the
    # stubbed _send_chat_completion never touches it.
    import httpx

    adapter._http = httpx.AsyncClient(base_url="http://stub.invalid")
    return adapter


@pytest.mark.asyncio
async def test_two_reads_run_in_parallel_when_fanout_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = await _make_adapter(max_parallel=3)

    # Script: turn 1 returns two read tool calls; turn 2 returns final.
    turns = iter(
        [
            _chat_completion_with_tools(
                [_tc("read_a", "c1"), _tc("read_b", "c2")]
            ),
            _chat_completion_final("done"),
        ]
    )

    async def _stub_send(*, payload, timeout):  # type: ignore[override]
        return next(turns)

    monkeypatch.setattr(adapter, "_send_chat_completion", _stub_send)

    per_call = 0.1
    executor = _SleepyExecutor(delay=per_call)
    agent = AgentHandle(
        agent_id="t", system_prompt="", model="stub"
    )

    t0 = time.monotonic()
    result: LlmSessionResult = await adapter.execute_with_tools(
        agent, "go", executor, timeout_seconds=5
    )
    elapsed = time.monotonic() - t0

    assert result.success is True
    # Sequential baseline is 2 × per_call = 0.2s. Parallel should be
    # ~per_call; we give it 1.6× headroom before failing for flake.
    assert elapsed < per_call * 1.6, (
        f"expected parallel (< {per_call * 1.6:.3f}s), got {elapsed:.3f}s"
    )
    assert len(executor.call_order) == 2

    await adapter._http.aclose()


@pytest.mark.asyncio
async def test_sequential_fallback_when_fanout_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature-off (``max_parallel_tool_calls=None``) keeps the old
    sequential behaviour — walltime ≈ 2× per-call."""
    adapter = await _make_adapter(max_parallel=None)

    turns = iter(
        [
            _chat_completion_with_tools(
                [_tc("read_a", "c1"), _tc("read_b", "c2")]
            ),
            _chat_completion_final("done"),
        ]
    )

    async def _stub_send(*, payload, timeout):
        return next(turns)

    monkeypatch.setattr(adapter, "_send_chat_completion", _stub_send)

    per_call = 0.08
    executor = _SleepyExecutor(delay=per_call)
    agent = AgentHandle(
        agent_id="t", system_prompt="", model="stub"
    )

    t0 = time.monotonic()
    result = await adapter.execute_with_tools(
        agent, "go", executor, timeout_seconds=5
    )
    elapsed = time.monotonic() - t0
    assert result.success is True
    # Sequential path: elapsed must exceed the parallel upper bound.
    assert elapsed >= per_call * 1.6, (
        f"expected sequential (>= {per_call * 1.6:.3f}s), "
        f"got {elapsed:.3f}s"
    )

    await adapter._http.aclose()


@pytest.mark.asyncio
async def test_world_call_serializes_within_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed ``[read, world, read]``: the world call must not run in
    parallel with either read; total walltime ≈ 3× per-call."""
    adapter = await _make_adapter(max_parallel=3)
    turns = iter(
        [
            _chat_completion_with_tools(
                [
                    _tc("read_a", "c1"),
                    _tc("world_w", "c2"),
                    _tc("read_b", "c3"),
                ]
            ),
            _chat_completion_final("done"),
        ]
    )

    async def _stub_send(*, payload, timeout):
        return next(turns)

    monkeypatch.setattr(adapter, "_send_chat_completion", _stub_send)

    per_call = 0.07
    executor = _SleepyExecutor(delay=per_call)
    agent = AgentHandle(
        agent_id="t", system_prompt="", model="stub"
    )

    t0 = time.monotonic()
    result = await adapter.execute_with_tools(
        agent, "go", executor, timeout_seconds=5
    )
    elapsed = time.monotonic() - t0
    assert result.success is True
    # Three serial groups (each size 1) — ~3 × per_call.
    assert elapsed >= per_call * 2.4
    # And not MORE than sequential with some slack (flake guard).
    assert elapsed < per_call * 5.0

    await adapter._http.aclose()


@pytest.mark.asyncio
async def test_tool_messages_preserve_request_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fan-out must NOT reorder ``tool_call_id`` → tool message
    mapping. This is a protocol correctness test, not a timing test.
    We record the messages passed to ``_send_chat_completion`` on
    turn 2 and assert the tool-messages arrive in request order
    regardless of which one finishes first."""
    adapter = await _make_adapter(max_parallel=3)

    # read_a sleeps longer than read_b so completion order ≠ request
    # order. Without order-preserving post-processing the tool
    # messages would swap.
    class _OrderedSleepy(_SleepyExecutor):
        async def execute_tool(self, tool_name, arguments):
            if tool_name == "read_a":
                await asyncio.sleep(0.12)
            else:
                await asyncio.sleep(0.02)
            self.call_order.append((tool_name, 0.0))
            return {"ok": True, "tool": tool_name}

    executor = _OrderedSleepy(delay=0.0)

    captured: list[dict[str, Any]] = []

    async def _stub_send(*, payload, timeout):
        captured.append(payload)
        if len(captured) == 1:
            return _chat_completion_with_tools(
                [_tc("read_a", "c1"), _tc("read_b", "c2")]
            )
        return _chat_completion_final("done")

    monkeypatch.setattr(adapter, "_send_chat_completion", _stub_send)
    agent = AgentHandle(
        agent_id="t", system_prompt="", model="stub"
    )
    await adapter.execute_with_tools(
        agent, "go", executor, timeout_seconds=5
    )

    assert len(captured) == 2
    msgs = captured[1]["messages"]
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"], (
        "tool messages reordered — protocol violation"
    )

    await adapter._http.aclose()


# ---------------------------------------------------------------------------
# H1 — built-in dispatch concurrency_group resolver
# ---------------------------------------------------------------------------


def test_default_dispatch_concurrency_group_groups_by_role_name() -> None:
    """Two ``dispatch_role_agent`` calls with the same role_name should
    map to the same group label regardless of other args, so
    ``partition_by_effect`` serialises them even when neither call site
    supplied a resolver."""
    from feishu_agent.core.llm_agent_adapter import (
        _default_dispatch_concurrency_group,
    )

    import json as _json

    tc1 = {
        "id": "x1",
        "function": {
            "name": "dispatch_role_agent",
            "arguments": _json.dumps({"role_name": "developer", "task": "a"}),
        },
    }
    tc2 = {
        "id": "x2",
        "function": {
            "name": "dispatch_role_agent",
            "arguments": _json.dumps({"role_name": "developer", "task": "b"}),
        },
    }
    tc_other = {
        "id": "x3",
        "function": {
            "name": "dispatch_role_agent",
            "arguments": _json.dumps({"role_name": "reviewer", "task": "c"}),
        },
    }
    assert _default_dispatch_concurrency_group(tc1) == "dispatch:developer"
    assert _default_dispatch_concurrency_group(tc2) == "dispatch:developer"
    assert _default_dispatch_concurrency_group(tc_other) == "dispatch:reviewer"


def test_default_dispatch_concurrency_group_honors_explicit_override() -> None:
    from feishu_agent.core.llm_agent_adapter import (
        _default_dispatch_concurrency_group,
    )
    import json as _json

    tc = {
        "id": "x",
        "function": {
            "name": "dispatch_role_agent",
            "arguments": _json.dumps(
                {"role_name": "developer", "concurrency_group": "shared-bitable"}
            ),
        },
    }
    assert (
        _default_dispatch_concurrency_group(tc)
        == "dispatch:shared-bitable"
    )


def test_default_dispatch_concurrency_group_returns_none_for_other_tools() -> None:
    from feishu_agent.core.llm_agent_adapter import (
        _default_dispatch_concurrency_group,
    )
    import json as _json

    assert (
        _default_dispatch_concurrency_group(
            {
                "id": "x",
                "function": {
                    "name": "read_file",
                    "arguments": _json.dumps({"path": "x"}),
                },
            }
        )
        is None
    )


def test_default_dispatch_concurrency_group_handles_bad_json() -> None:
    from feishu_agent.core.llm_agent_adapter import (
        _default_dispatch_concurrency_group,
    )

    # Malformed arguments must not crash the adapter.
    assert (
        _default_dispatch_concurrency_group(
            {
                "id": "x",
                "function": {
                    "name": "dispatch_role_agent",
                    "arguments": "{not-json",
                },
            }
        )
        is None
    )


@pytest.mark.asyncio
async def test_fanout_prefetch_exception_is_captured_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M4 — an executor that raises during fan-out prefetch should
    surface the exception as a tool result with ``error`` set so the
    downstream loop treats it as a normal tool failure (not a silent
    swallow). Regression guard against the ``pragma: no cover`` branch.
    """

    class _ExplodingExecutor:
        async def execute_tool(self, name: str, args: dict) -> dict:
            raise RuntimeError("boom")

        def tool_specs(self) -> list[AgentToolSpec]:
            return [
                AgentToolSpec(
                    name="read_a",
                    description="",
                    input_schema={"type": "object"},
                    effect="read",
                )
            ]

    adapter = await _make_adapter(max_parallel=2)

    captured: list[dict[str, Any]] = []

    async def _stub_send(*, payload, timeout):
        captured.append(payload)
        if len(captured) == 1:
            return _chat_completion_with_tools([_tc("read_a", "c1")])
        return _chat_completion_final("done")

    monkeypatch.setattr(adapter, "_send_chat_completion", _stub_send)
    agent = AgentHandle(agent_id="t", system_prompt="", model="stub")
    result = await adapter.execute_with_tools(
        agent, "go", _ExplodingExecutor(), timeout_seconds=5
    )
    assert isinstance(result, LlmSessionResult)

    # The second LLM round must carry a tool message whose body reports
    # the executor error (error ≠ missing-field) so the validation-loop
    # circuit breaker doesn't mistake it for a schema violation.
    tool_msgs = [
        m for m in captured[1]["messages"] if m.get("role") == "tool"
    ]
    assert tool_msgs, "no tool message produced — prefetch swallowed the error"
    body = tool_msgs[0].get("content") or ""
    assert "boom" in body, "tool error payload missing exception message"

    await adapter._http.aclose()
