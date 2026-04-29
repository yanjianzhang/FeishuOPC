"""Unit tests for the Bedrock transport layer.

These tests cover the two conversion seams — OpenAI payload → Anthropic
kwargs, Anthropic Message → OpenAI-shape dict — plus the ``send`` glue
via a fully mocked ``AsyncAnthropicBedrock`` client.

We deliberately do NOT exercise real Bedrock credentials here; that
belongs in an opt-in smoke test under ``.larkagent/scripts``. The goal
of this file is to lock down the schema mapping so drift in either
direction (OpenAI-side or Anthropic-side) trips a test before it trips
the PM bot in production.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any
from unittest.mock import AsyncMock

import pytest

from feishu_agent.providers.bedrock_transport import (
    anthropic_message_to_openai,
    openai_payload_to_anthropic,
    send_bedrock_chat_completion,
)
from feishu_agent.providers.llm_provider_pool import LlmProviderConfig

# ---------------------------------------------------------------------------
# Payload conversion: OpenAI → Anthropic
# ---------------------------------------------------------------------------


def test_system_messages_are_extracted_to_top_level():
    payload: dict[str, Any] = {
        "model": "test",
        "messages": [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
        ],
        "max_tokens": 100,
    }
    kwargs = openai_payload_to_anthropic(payload, default_model="fallback-model")
    assert kwargs["system"] == "you are helpful"
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]
    assert kwargs["model"] == "test"
    assert kwargs["max_tokens"] == 100


def test_multiple_system_messages_are_concatenated():
    """OpenAI allows multiple system messages; Anthropic wants one
    top-level ``system`` string. We concatenate with double newline
    so operator-authored system prompts retain paragraph structure."""
    payload: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": "rule 1"},
            {"role": "system", "content": "rule 2"},
            {"role": "user", "content": "ok"},
        ]
    }
    kwargs = openai_payload_to_anthropic(payload, default_model="m")
    assert kwargs["system"] == "rule 1\n\nrule 2"


def test_assistant_tool_calls_become_tool_use_blocks():
    """An assistant turn with ``content=null`` + ``tool_calls`` must
    project into an Anthropic content list containing ``tool_use`` blocks,
    not a ``content=null`` message (Anthropic rejects that)."""
    payload: dict[str, Any] = {
        "messages": [
            {"role": "user", "content": "list files"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "list_files",
                            "arguments": json.dumps({"path": "/tmp"}),
                        },
                    }
                ],
            },
        ]
    }
    kwargs = openai_payload_to_anthropic(payload, default_model="m")
    assert kwargs["messages"][1] == {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "call_1",
                "name": "list_files",
                "input": {"path": "/tmp"},
            }
        ],
    }


def test_assistant_text_plus_tool_calls_preserves_order():
    """When the assistant turn has BOTH text and tool_calls, Anthropic
    expects the text block first, then tool_use blocks. Reversing the
    order changes the semantic meaning (tool result vs. commentary)."""
    payload: dict[str, Any] = {
        "messages": [
            {"role": "user", "content": "x"},
            {
                "role": "assistant",
                "content": "I'll check.",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{}"},
                    }
                ],
            },
        ]
    }
    kwargs = openai_payload_to_anthropic(payload, default_model="m")
    assistant_blocks = kwargs["messages"][1]["content"]
    assert assistant_blocks[0] == {"type": "text", "text": "I'll check."}
    assert assistant_blocks[1]["type"] == "tool_use"


def test_consecutive_tool_messages_merge_into_one_user_turn():
    """Two tool results in a row need to collapse to a single Anthropic
    ``user`` turn with a list of ``tool_result`` blocks. Emitting each
    as its own turn would put tool results in places Anthropic rejects."""
    payload: dict[str, Any] = {
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "a",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{}"},
                    },
                    {
                        "id": "b",
                        "type": "function",
                        "function": {"name": "g", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "a", "content": "ra"},
            {"role": "tool", "tool_call_id": "b", "content": "rb"},
            {"role": "user", "content": "ok"},
        ]
    }
    kwargs = openai_payload_to_anthropic(payload, default_model="m")
    # The two tool turns merge into one user turn with two tool_result blocks.
    tool_turn = kwargs["messages"][1]
    assert tool_turn["role"] == "user"
    assert tool_turn["content"] == [
        {"type": "tool_result", "tool_use_id": "a", "content": "ra"},
        {"type": "tool_result", "tool_use_id": "b", "content": "rb"},
    ]
    # And the trailing user message is its own turn after the merge.
    assert kwargs["messages"][2] == {"role": "user", "content": "ok"}


def test_tools_are_translated_including_input_schema_rename():
    payload: dict[str, Any] = {
        "messages": [{"role": "user", "content": "x"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "do_it",
                    "description": "does it",
                    "parameters": {
                        "type": "object",
                        "properties": {"arg": {"type": "string"}},
                    },
                },
            }
        ],
        "tool_choice": "auto",
    }
    kwargs = openai_payload_to_anthropic(payload, default_model="m")
    assert kwargs["tools"] == [
        {
            "name": "do_it",
            "description": "does it",
            "input_schema": {
                "type": "object",
                "properties": {"arg": {"type": "string"}},
            },
        }
    ]
    assert kwargs["tool_choice"] == {"type": "auto"}


def test_tool_choice_forced_function_is_translated():
    payload: dict[str, Any] = {
        "messages": [{"role": "user", "content": "x"}],
        "tools": [
            {
                "type": "function",
                "function": {"name": "force_me", "parameters": {}},
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "force_me"}},
    }
    kwargs = openai_payload_to_anthropic(payload, default_model="m")
    assert kwargs["tool_choice"] == {"type": "tool", "name": "force_me"}


def test_missing_max_tokens_uses_default():
    """Anthropic rejects requests without ``max_tokens``; we must supply
    a sensible default when the caller omits it."""
    payload: dict[str, Any] = {
        "messages": [{"role": "user", "content": "x"}],
    }
    kwargs = openai_payload_to_anthropic(
        payload, default_model="m", default_max_tokens=2048
    )
    assert kwargs["max_tokens"] == 2048


def test_malformed_tool_arguments_do_not_crash():
    """A garbled ``tool_calls[].function.arguments`` (non-JSON) from a
    previous assistant turn should not kill the fallback conversion —
    we wrap the raw string so Bedrock sees SOMETHING and can reject
    it normally, instead of crashing this module with a decode error."""
    payload: dict[str, Any] = {
        "messages": [
            {"role": "user", "content": "x"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "f", "arguments": "<not json>"},
                    }
                ],
            },
        ]
    }
    kwargs = openai_payload_to_anthropic(payload, default_model="m")
    tool_use = kwargs["messages"][1]["content"][0]
    assert tool_use["type"] == "tool_use"
    assert tool_use["input"] == {"_raw": "<not json>"}


# ---------------------------------------------------------------------------
# Response conversion: Anthropic → OpenAI
# ---------------------------------------------------------------------------


class _FakeBlock:
    """Stand-in for SDK Pydantic content blocks (text / tool_use)."""

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeMessage:
    def __init__(
        self,
        *,
        content: list[_FakeBlock],
        stop_reason: str,
        usage: _FakeUsage,
    ) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage


def test_text_only_response_maps_to_assistant_content_string():
    msg = _FakeMessage(
        content=[_FakeBlock(type="text", text="hello world")],
        stop_reason="end_turn",
        usage=_FakeUsage(10, 20),
    )
    result = anthropic_message_to_openai(msg)
    assert result["choices"][0]["message"]["content"] == "hello world"
    assert "tool_calls" not in result["choices"][0]["message"]
    assert result["choices"][0]["finish_reason"] == "stop"
    assert result["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
    }


def test_tool_use_response_maps_to_openai_tool_calls():
    msg = _FakeMessage(
        content=[
            _FakeBlock(type="text", text="let me call it"),
            _FakeBlock(
                type="tool_use",
                id="tool_abc",
                name="list_files",
                input={"path": "/tmp"},
            ),
        ],
        stop_reason="tool_use",
        usage=_FakeUsage(5, 7),
    )
    result = anthropic_message_to_openai(msg)
    choice = result["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "let me call it"
    assert choice["message"]["tool_calls"] == [
        {
            "id": "tool_abc",
            "type": "function",
            "function": {
                "name": "list_files",
                "arguments": json.dumps({"path": "/tmp"}, ensure_ascii=False),
            },
        }
    ]


def test_max_tokens_stop_reason_maps_to_length():
    msg = _FakeMessage(
        content=[_FakeBlock(type="text", text="truncated")],
        stop_reason="max_tokens",
        usage=_FakeUsage(1, 1),
    )
    result = anthropic_message_to_openai(msg)
    assert result["choices"][0]["finish_reason"] == "length"


def test_accepts_dict_shaped_message_for_test_fixtures():
    """Allow callers to pass a plain dict instead of the SDK Pydantic
    object. Makes it trivial to build round-trip tests without importing
    the SDK at test time."""
    msg = {
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 2, "output_tokens": 3},
    }
    result = anthropic_message_to_openai(msg)
    assert result["choices"][0]["message"]["content"] == "ok"
    assert result["usage"]["total_tokens"] == 5


# ---------------------------------------------------------------------------
# send_bedrock_chat_completion: glue + SDK mocking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_uses_async_anthropic_bedrock_with_translated_kwargs(
    monkeypatch,
):
    """End-to-end: payload → converter → fake SDK → converter → dict.

    We stub ``anthropic`` entirely so the test works even on boxes that
    don't have the package's boto3 extras installed.
    """
    captured_init: dict[str, Any] = {}
    captured_create_kwargs: dict[str, Any] = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            captured_init.update(kwargs)
            self.messages = types.SimpleNamespace(create=AsyncMock(
                return_value=_FakeMessage(
                    content=[_FakeBlock(type="text", text="pong")],
                    stop_reason="end_turn",
                    usage=_FakeUsage(3, 4),
                )
            ))

        async def close(self) -> None:
            pass

    async def _record_create(**kwargs: Any) -> Any:
        captured_create_kwargs.update(kwargs)
        return _FakeMessage(
            content=[_FakeBlock(type="text", text="pong")],
            stop_reason="end_turn",
            usage=_FakeUsage(3, 4),
        )

    class _RecordingClient(_FakeAsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.messages = types.SimpleNamespace(create=_record_create)

    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.AsyncAnthropicBedrock = _RecordingClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    provider = LlmProviderConfig(
        name="secondary",
        base_url="",
        api_key="",
        model="arn:aws:bedrock:us-west-2:0:application-inference-profile/xyz",
        timeout_seconds=60.0,
        transport="anthropic_bedrock",
        aws_region="us-west-2",
        aws_access_key_id="AKIA_TEST",
        aws_secret_access_key="secret",
    )
    payload: dict[str, Any] = {
        "model": "primary-override",
        "messages": [
            {"role": "system", "content": "you are a bot"},
            {"role": "user", "content": "ping"},
        ],
        "max_tokens": 64,
        "temperature": 0.2,
    }
    result = await send_bedrock_chat_completion(
        provider, payload=payload, timeout=60.0
    )

    assert captured_init == {
        "aws_region": "us-west-2",
        "aws_access_key": "AKIA_TEST",
        "aws_secret_key": "secret",
        "timeout": 60.0,
    }
    assert captured_create_kwargs["model"] == "primary-override"
    assert captured_create_kwargs["system"] == "you are a bot"
    assert captured_create_kwargs["max_tokens"] == 64
    assert captured_create_kwargs["temperature"] == pytest.approx(0.2)
    assert captured_create_kwargs["messages"] == [
        {"role": "user", "content": "ping"}
    ]
    assert result["choices"][0]["message"]["content"] == "pong"
    assert result["usage"]["total_tokens"] == 7


@pytest.mark.asyncio
async def test_send_falls_back_to_provider_model_when_payload_omits_it(
    monkeypatch,
):
    """When ``payload["model"]`` is missing, the Bedrock call uses the
    provider's configured model. Important because the adapter's
    ``_send_chat_completion`` hands us the raw payload — if the caller
    forgot to set ``model`` we'd otherwise call Bedrock with ``None``
    and get a 400."""

    captured: dict[str, Any] = {}

    async def _record_create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _FakeMessage(
            content=[_FakeBlock(type="text", text="x")],
            stop_reason="end_turn",
            usage=_FakeUsage(1, 1),
        )

    class _C:
        def __init__(self, **_kwargs: Any) -> None:
            self.messages = types.SimpleNamespace(create=_record_create)

        async def close(self) -> None:
            pass

    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.AsyncAnthropicBedrock = _C  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    provider = LlmProviderConfig(
        name="secondary",
        base_url="",
        api_key="",
        model="provider-default-model",
        transport="anthropic_bedrock",
        aws_region="us-west-2",
        aws_access_key_id="k",
        aws_secret_access_key="s",
    )
    payload: dict[str, Any] = {
        "messages": [{"role": "user", "content": "x"}],
    }
    await send_bedrock_chat_completion(provider, payload=payload, timeout=10.0)
    assert captured["model"] == "provider-default-model"
