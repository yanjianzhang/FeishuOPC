"""Tier-2 integration tests for ``LlmAgentAdapter.execute_with_tools``.

Verifies the three new behaviors the tool loop must guarantee:

1. ``hook_bus`` receives ``on_session_start`` + ``pre_llm_call`` +
   ``post_llm_call`` + ``on_tool_call`` + ``on_session_end`` in the
   right order with a single session.
2. A cancel request between tool calls stops the loop with
   ``stop_reason=cancelled`` on the very next checkpoint.
3. Hooks and the legacy ``on_tool_call`` observer both fire — the new
   bus is additive, not a replacement.

We drive the adapter against a stubbed ``_send_chat_completion`` so we
don't need a real HTTP server.
"""

from __future__ import annotations

from typing import Any

import pytest

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.core.cancel_token import CancelToken
from feishu_agent.core.hook_bus import HookBus
from feishu_agent.core.llm_agent_adapter import (
    AgentHandle,
    LlmAgentAdapter,
)


class _FakeTool:
    """A tool executor with one tool (``ping``) that returns a fixed
    payload. Kept deliberately trivial — the interesting behavior
    under test is the loop, not the tool."""

    def tool_specs(self):
        return [
            AgentToolSpec(
                name="ping",
                description="ping",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    async def execute_tool(self, tool_name, arguments):
        return {"pong": arguments.get("v", 0)}


def _make_adapter(completions: list[dict[str, Any]]) -> LlmAgentAdapter:
    """Build an adapter whose ``_send_chat_completion`` returns the
    next pre-canned response on each call. The stub bypasses httpx
    entirely — letting us exercise every adapter branch without a
    live server."""
    adapter = LlmAgentAdapter(
        llm_base_url="http://example.invalid",
        llm_api_key="k",
        default_model="m",
        timeout=30,
    )
    # Pretend we're connected so ``execute_with_tools`` doesn't bail.
    adapter._http = object()  # type: ignore[assignment]
    call_count = {"n": 0}

    async def _stub_send(*, payload, timeout):
        i = call_count["n"]
        call_count["n"] += 1
        return completions[i]

    adapter._send_chat_completion = _stub_send  # type: ignore[assignment]
    return adapter


@pytest.mark.asyncio
async def test_hook_bus_receives_full_lifecycle():
    """One tool-call turn + one final completion = expected events."""
    completions = [
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {
                                    "name": "ping",
                                    "arguments": '{"v": 3}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23},
        },
    ]
    adapter = _make_adapter(completions)
    bus = HookBus()
    events: list[tuple[str, dict[str, Any]]] = []

    def record(event, payload):
        events.append((event, payload))

    bus.subscribe_many(
        [
            "on_session_start",
            "pre_llm_call",
            "post_llm_call",
            "on_tool_call",
            "on_session_end",
        ],
        record,
    )

    agent = AgentHandle(agent_id="a", system_prompt="sys", model="m")
    result = await adapter.execute_with_tools(
        agent,
        "run it",
        _FakeTool(),
        timeout_seconds=30,
        hook_bus=bus,
        trace_id="trace-1",
    )
    assert result.success is True

    ordered = [name for name, _ in events]
    # Expect exactly one session_start, one session_end, one tool_call,
    # two llm-call pairs. Interleave pattern:
    #   start, pre, post, on_tool_call, pre, post, end
    assert ordered == [
        "on_session_start",
        "pre_llm_call",
        "post_llm_call",
        "on_tool_call",
        "pre_llm_call",
        "post_llm_call",
        "on_session_end",
    ]

    # on_session_end must carry ok=True.
    _, end_payload = events[-1]
    assert end_payload["ok"] is True
    assert end_payload["trace_id"] == "trace-1"


@pytest.mark.asyncio
async def test_cancel_between_tool_calls_short_circuits_loop():
    """After the first tool call completes, we flip the cancel token.
    The loop's 'between tool calls' checkpoint must catch it before
    running another tool / LLM call."""
    completions = [
        {
            # Two tool calls in the same turn so the 'between tool
            # calls' checkpoint is exercised.
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {"name": "ping", "arguments": "{}"},
                            },
                            {
                                "id": "c2",
                                "function": {"name": "ping", "arguments": "{}"},
                            },
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"total_tokens": 5},
        }
    ]
    adapter = _make_adapter(completions)
    token = CancelToken()
    bus = HookBus()
    calls_observed: list[str] = []

    def on_tool(event, payload):
        calls_observed.append(payload["tool_name"])
        # Trigger cancel right after the first tool fires.
        if len(calls_observed) == 1:
            token.cancel(reason="test_user")

    bus.subscribe("on_tool_call", on_tool)

    agent = AgentHandle(agent_id="a", system_prompt="sys", model="m")
    result = await adapter.execute_with_tools(
        agent,
        "run",
        _FakeTool(),
        timeout_seconds=30,
        hook_bus=bus,
        cancel_token=token,
        trace_id="t",
    )
    assert result.success is False
    assert result.stop_reason == "cancelled"
    # Only the first tool fired; the second got short-circuited.
    assert calls_observed == ["ping"]


@pytest.mark.asyncio
async def test_legacy_on_tool_call_observer_still_fires_alongside_bus():
    """Migrating to HookBus must not break callers still wired to the
    legacy ``on_tool_call`` observer. Both should fire."""
    completions = [
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {"name": "ping", "arguments": "{}"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"total_tokens": 1},
        },
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"total_tokens": 2},
        },
    ]
    adapter = _make_adapter(completions)
    bus_calls: list[str] = []
    observer_calls: list[str] = []
    bus = HookBus()
    bus.subscribe("on_tool_call", lambda e, p: bus_calls.append(p["tool_name"]))

    async def legacy_observer(name, args, result, dur):
        observer_calls.append(name)

    agent = AgentHandle(agent_id="a", system_prompt="", model="m")
    await adapter.execute_with_tools(
        agent,
        "q",
        _FakeTool(),
        timeout_seconds=30,
        hook_bus=bus,
        on_tool_call=legacy_observer,
    )
    assert bus_calls == ["ping"]
    assert observer_calls == ["ping"]


# ---------------------------------------------------------------------------
# Tool-argument circuit breaker
#
# Background: on 2026-04-19, a developer sub-session dispatched by the
# tech lead for Story 3-2 tripped an LLM failure mode where the
# upstream model repeatedly emitted ``write_project_code_batch({})``
# and then ``write_project_code({})`` — valid tool-call JSON but
# stripped of its required ``files`` / ``content`` fields. Each
# attempt returned a multi-line pydantic ValidationError, which the
# model then ignored and re-issued the same empty call. The session
# burned 5 batch retries + 6 single-file retries before the upstream
# provider eventually timed out at 740s, at which point the Feishu
# user saw only "❌ developer failed (connection timeout)" without
# any hint that the root cause was an arg-loop, not network.
#
# These tests pin the circuit-breaker behaviour that prevents that
# failure mode from ever wasting 12 minutes of user time again:
#
#   * A pydantic ``Field required`` error gets rewritten into a
#     structured ``TOOL_CALL_ARG_MISSING`` payload with actionable
#     guidance (what fields are missing, what the model sent,
#     what to do differently). Helpful independently of the loop
#     detector.
#   * If the same (tool, missing-fields) combination fails
#     ``TOOL_ARG_MISSING_LOOP_BUDGET`` (=3) times in a row, the
#     session aborts early with ``stop_reason="tool_arg_loop"``
#     so the Feishu user sees a useful error instead of waiting
#     out the remote timeout.
#   * A successful (or differently-failing) tool call resets the
#     loop counter — the breaker is specifically for "stuck on the
#     same thing", not "occasionally errors".
# ---------------------------------------------------------------------------


class _FailingTool:
    """Tool that always raises a synthetic pydantic-style
    ``Field required`` error. Mimics what happens when the LLM emits
    ``write_project_code({})`` and the executor's Pydantic validator
    rejects it."""

    def __init__(self, missing: list[str]) -> None:
        self._missing = missing

    def tool_specs(self):
        return [
            AgentToolSpec(
                name="write_thing",
                description="writes a thing",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    async def execute_tool(self, tool_name, arguments):
        lines = [f"{len(self._missing)} validation errors for WriteThingArgs"]
        for field_name in self._missing:
            lines.append(field_name)
            lines.append(
                "  Field required [type=missing, input_value={}, "
                "input_type=dict]"
            )
        raise ValueError("\n".join(lines))


def _tool_call_turn(name: str, args_json: str, call_id: str = "c") -> dict[str, Any]:
    """Helper: a single completion that issues one tool call."""
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "function": {"name": name, "arguments": args_json},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"total_tokens": 1},
    }


@pytest.mark.asyncio
async def test_missing_required_field_error_is_rewritten_for_llm():
    """First ``Field required`` failure: error is replaced with a
    structured ``TOOL_CALL_ARG_MISSING`` payload carrying the missing
    fields, the bad args, and recovery guidance. Session keeps
    running."""
    completions = [
        _tool_call_turn("write_thing", "{}", call_id="c1"),
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "gave up"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"total_tokens": 2},
        },
    ]
    adapter = _make_adapter(completions)
    bus = HookBus()
    recorded: list[dict[str, Any]] = []
    bus.subscribe("on_tool_call", lambda e, p: recorded.append(p))

    agent = AgentHandle(agent_id="a", system_prompt="", model="m")
    result = await adapter.execute_with_tools(
        agent,
        "q",
        _FailingTool(missing=["files", "reason"]),
        timeout_seconds=30,
        hook_bus=bus,
    )
    assert result.success is True  # session ends normally
    assert len(recorded) == 1
    res = recorded[0]["result"]
    assert res["error"] == "TOOL_CALL_ARG_MISSING"
    assert res["tool"] == "write_thing"
    assert set(res["missing_required_fields"]) == {"files", "reason"}
    assert res["you_sent_arguments"] == {}
    # Guidance string must suggest the documented recovery paths so
    # the model has a concrete next step.
    g = res["guidance"]
    assert "DO NOT re-issue" in g
    assert "write_role_artifact" in g
    assert "single-file" in g


@pytest.mark.asyncio
async def test_three_consecutive_missing_field_failures_abort_session():
    """Pins the 740s-developer-timeout incident: on the 3rd identical
    ``Field required`` failure, the tool loop aborts the session with
    ``stop_reason=tool_arg_loop`` instead of letting the model keep
    spinning until the remote provider times out."""
    completions = [
        _tool_call_turn("write_thing", "{}", call_id="c1"),
        _tool_call_turn("write_thing", "{}", call_id="c2"),
        _tool_call_turn("write_thing", "{}", call_id="c3"),
        # If the circuit breaker DIDN'T fire, the loop would consume
        # this 4th completion too. Having one here makes the test
        # fail loudly on regression rather than silently via
        # "IndexError: pop from empty list" in the stub.
        _tool_call_turn("write_thing", "{}", call_id="c4"),
    ]
    adapter = _make_adapter(completions)
    agent = AgentHandle(agent_id="a", system_prompt="", model="m")
    result = await adapter.execute_with_tools(
        agent,
        "q",
        _FailingTool(missing=["files"]),
        timeout_seconds=30,
    )
    assert result.success is False
    assert result.stop_reason == "tool_arg_loop"
    assert "write_thing" in (result.error_message or "")
    assert "files" in (result.error_message or "")


@pytest.mark.asyncio
async def test_different_tool_or_different_missing_fields_resets_counter():
    """The breaker targets ``stuck on the same call``, not ``errored
    a few times across the session``. Switching tools (or switching
    WHICH fields are missing) resets the counter so a session that is
    actually making progress isn't killed."""

    class _MixedFailingTool:
        """First two calls miss ``files``; next two miss ``content``;
        next two miss ``files`` again. No consecutive run hits the
        budget of 3."""

        def __init__(self) -> None:
            self._idx = 0

        def tool_specs(self):
            return [
                AgentToolSpec(
                    name="write_thing",
                    description="w",
                    input_schema={"type": "object", "properties": {}},
                )
            ]

        async def execute_tool(self, tool_name, arguments):
            pattern = ["files", "files", "content", "content", "files", "files"]
            missing = pattern[self._idx]
            self._idx += 1
            raise ValueError(
                f"1 validation error for WriteThingArgs\n"
                f"{missing}\n"
                f"  Field required [type=missing, input_value={{}}, "
                f"input_type=dict]"
            )

    completions = [_tool_call_turn("write_thing", "{}", call_id=f"c{i}") for i in range(6)]
    completions.append(
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"total_tokens": 2},
        }
    )
    adapter = _make_adapter(completions)
    agent = AgentHandle(agent_id="a", system_prompt="", model="m")
    result = await adapter.execute_with_tools(
        agent,
        "q",
        _MixedFailingTool(),
        timeout_seconds=30,
    )
    # No consecutive (tool, missing) ran 3x; session completed normally.
    assert result.success is True
    assert result.stop_reason != "tool_arg_loop"


def test_extract_missing_required_fields_parses_pydantic_v2_output():
    """Unit test for the parser that underpins the breaker. Keeps the
    regex honest against pydantic's current formatting — if pydantic
    ever changes the shape, this test will catch it before the
    breaker silently degrades to the fall-through path."""
    from feishu_agent.core.llm_agent_adapter import (
        _extract_missing_required_fields,
    )

    err = (
        "2 validation errors for WriteProjectCodeBatchArgs\n"
        "files\n"
        "  Field required [type=missing, input_value={}, input_type=dict]\n"
        "reason\n"
        "  Field required [type=missing, input_value={}, input_type=dict]\n"
    )
    assert _extract_missing_required_fields(err) == ["files", "reason"]

    # Non-"Field required" validation errors don't hit the breaker —
    # the model usually self-corrects from a concrete type error.
    err2 = (
        "1 validation error for WriteProjectCodeArgs\n"
        "relative_path\n"
        "  Input should be a valid string [type=string_type, "
        "input_value=123, input_type=int]\n"
    )
    assert _extract_missing_required_fields(err2) == []

    # Empty / non-pydantic inputs return [] — defensive.
    assert _extract_missing_required_fields("") == []
    assert _extract_missing_required_fields("random error") == []
