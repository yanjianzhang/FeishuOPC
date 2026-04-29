"""AWS Bedrock transport for the LLM provider pool.

Why this module exists
----------------------
``LlmProviderPool`` and ``LlmAgentAdapter`` speak OpenAI-compatible
chat completions (``/chat/completions`` + ``Authorization: Bearer``).
AWS Bedrock doesn't — it uses SigV4 signing, and its message and tool
schemas are Anthropic-native (``messages.create`` with content blocks,
``tool_use``/``tool_result`` blocks, ``system`` separated from
``messages``, ``input_schema`` instead of ``parameters``).

We don't want Bedrock-specific wiring to bleed into the adapter. This
module is the narrow translation layer between the two worlds:

- ``openai_payload_to_anthropic`` — take the same dict the adapter
  builds for the OpenAI relay and translate it into kwargs that the
  ``anthropic.AsyncAnthropicBedrock.messages.create`` call accepts.
- ``anthropic_message_to_openai`` — take the ``Message`` returned by
  the SDK and project it back into the OpenAI ``{"choices":[{"message":
  ..., "finish_reason": ...}], "usage": ...}`` shape the adapter's
  downstream parser already handles.
- ``send_bedrock_chat_completion`` — wire the two together into the
  single callable the pool expects.

The idea is that the adapter keeps all of its post-processing code
(tool-call parsing, finish-reason handling, usage accounting) pointed
at an OpenAI-shape dict, and the pool sees an opaque ``send`` callback.
All Bedrock-specific knowledge lives here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from feishu_agent.providers.llm_provider_pool import LlmProviderConfig

logger = logging.getLogger(__name__)


# Anthropic's ``messages.create`` demands a ``max_tokens`` argument —
# there is no "default" like OpenAI has. If the caller didn't pass one,
# supply a generous but not unbounded ceiling so we don't trip Bedrock's
# hard per-request cap and get a 400. 8192 is the current published max
# for Claude-3.x Sonnet/Opus Bedrock application inference profiles.
_BEDROCK_DEFAULT_MAX_TOKENS = 8192


# OpenAI ``tool_choice`` → Anthropic ``tool_choice`` translation. OpenAI
# accepts ``"auto" | "none" | "required" | {"type":"function","function":{"name": ...}}``;
# Anthropic accepts ``{"type":"auto"} | {"type":"none"} | {"type":"any"} | {"type":"tool","name": ...}``.
# Wrapped in a helper because we'd otherwise duplicate this table wherever
# we touch a payload.
def _translate_tool_choice(choice: Any) -> dict[str, Any] | None:
    if choice is None:
        return None
    if isinstance(choice, str):
        mapping = {
            "auto": {"type": "auto"},
            "none": {"type": "none"},
            "required": {"type": "any"},
        }
        return mapping.get(choice, {"type": "auto"})
    if isinstance(choice, dict):
        # OpenAI's forced-function form:
        # ``{"type":"function","function":{"name":"<name>"}}``
        if choice.get("type") == "function":
            fn = choice.get("function") or {}
            name = fn.get("name")
            if name:
                return {"type": "tool", "name": str(name)}
    return {"type": "auto"}


def _translate_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not tools:
        return []
    out: list[dict[str, Any]] = []
    for tool in tools:
        # OpenAI shape: {"type":"function","function":{"name":..., "description":..., "parameters":{...}}}
        # Anthropic shape: {"name":..., "description":..., "input_schema":{...}}
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not fn:
            # Already in Anthropic shape? Accept that too — keeps the
            # conversion idempotent for callers that hand-build Bedrock
            # payloads in tests.
            if isinstance(tool, dict) and "input_schema" in tool:
                out.append(tool)
            continue
        name = fn.get("name")
        if not name:
            continue
        anthropic_tool: dict[str, Any] = {"name": str(name)}
        if fn.get("description"):
            anthropic_tool["description"] = str(fn["description"])
        # ``parameters`` is an OpenAI JSON schema; Bedrock uses the exact
        # same JSON-Schema dialect, so it's a direct rename (no field
        # conversion).
        params = fn.get("parameters")
        if isinstance(params, dict):
            anthropic_tool["input_schema"] = params
        else:
            # Anthropic requires ``input_schema`` to be a JSON schema
            # object; fall back to an empty-object schema so the tool is
            # still declarable.
            anthropic_tool["input_schema"] = {"type": "object", "properties": {}}
        out.append(anthropic_tool)
    return out


def _content_to_anthropic_text(value: Any) -> str:
    """Normalize a string-or-list ``content`` field into a single string.

    Used for assistant/tool message content that arrives in OpenAI shape
    — assistant may set ``content=null`` + ``tool_calls``, tool responses
    are always strings. Anthropic's tool_result wants a string or a list
    of content blocks; we keep it simple by stringifying.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                    continue
                # Unknown block shape — fall through to JSON
                parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(value)


def openai_payload_to_anthropic(
    payload: dict[str, Any],
    *,
    default_model: str,
    default_max_tokens: int = _BEDROCK_DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """Translate an OpenAI-shape chat-completions payload to Bedrock kwargs.

    The returned dict is fed directly into
    ``AsyncAnthropicBedrock.messages.create(**kwargs)``.

    Edge cases we deliberately handle:

    - **System messages** are pulled out of ``messages`` and concatenated
      (with ``\\n\\n``) into the top-level ``system`` parameter. Multiple
      system entries are uncommon but legal in OpenAI and Anthropic
      forbids them inside ``messages``.
    - **Assistant ``content=null`` with ``tool_calls``** becomes an
      assistant content list containing only ``tool_use`` blocks — the
      equivalent Anthropic turn.
    - **Consecutive ``role="tool"`` messages** are merged into a single
      ``{"role":"user","content":[tool_result, tool_result, ...]}``
      turn, because Anthropic requires tool results to be grouped on
      one user turn rather than appearing as standalone messages.
    - **``max_tokens`` missing** → fall back to ``default_max_tokens``.
      Anthropic rejects requests without it (400), so we must supply one.
    """
    messages = payload.get("messages") or []

    system_parts: list[str] = []
    anthropic_messages: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    def _flush_tool_results() -> None:
        if not pending_tool_results:
            return
        anthropic_messages.append(
            {"role": "user", "content": list(pending_tool_results)}
        )
        pending_tool_results.clear()

    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")

        if role == "system":
            _flush_tool_results()
            text = _content_to_anthropic_text(message.get("content"))
            if text:
                system_parts.append(text)
            continue

        if role == "tool":
            # Batch until we hit a non-tool message, then emit as one
            # user turn with a list of tool_result blocks.
            tool_call_id = message.get("tool_call_id") or ""
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": str(tool_call_id),
                    "content": _content_to_anthropic_text(message.get("content")),
                }
            )
            continue

        # Any non-tool, non-system message flushes pending tool results.
        _flush_tool_results()

        if role == "user":
            content = message.get("content")
            if isinstance(content, list):
                anthropic_messages.append({"role": "user", "content": content})
            else:
                anthropic_messages.append(
                    {"role": "user", "content": _content_to_anthropic_text(content)}
                )
            continue

        if role == "assistant":
            tool_calls = message.get("tool_calls") or []
            blocks: list[dict[str, Any]] = []
            text = _content_to_anthropic_text(message.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                raw_args = fn.get("arguments")
                parsed_args: Any
                if isinstance(raw_args, str):
                    try:
                        parsed_args = json.loads(raw_args) if raw_args else {}
                    except json.JSONDecodeError:
                        # Defensive: don't let a malformed tool-call
                        # string from a previous assistant turn crash
                        # the fallback. Pass through as a string blob —
                        # Bedrock will reject it with a 400, which the
                        # adapter surfaces as a normal LLM error.
                        parsed_args = {"_raw": raw_args}
                elif isinstance(raw_args, dict):
                    parsed_args = raw_args
                else:
                    parsed_args = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(tc.get("id") or ""),
                        "name": str(fn.get("name") or ""),
                        "input": parsed_args,
                    }
                )
            if not blocks:
                # Empty assistant turn would be rejected by Anthropic.
                # Drop it; the subsequent user/tool turn carries the real
                # request-response boundary.
                continue
            if len(blocks) == 1 and blocks[0]["type"] == "text":
                anthropic_messages.append(
                    {"role": "assistant", "content": blocks[0]["text"]}
                )
            else:
                anthropic_messages.append({"role": "assistant", "content": blocks})
            continue

    _flush_tool_results()

    kwargs: dict[str, Any] = {
        "model": payload.get("model") or default_model,
        "messages": anthropic_messages,
        "max_tokens": int(payload.get("max_tokens") or default_max_tokens),
    }
    if system_parts:
        kwargs["system"] = "\n\n".join(system_parts)
    if "temperature" in payload and payload["temperature"] is not None:
        kwargs["temperature"] = float(payload["temperature"])

    translated_tools = _translate_tools(payload.get("tools"))
    if translated_tools:
        kwargs["tools"] = translated_tools
        tc = _translate_tool_choice(payload.get("tool_choice", "auto"))
        if tc is not None:
            kwargs["tool_choice"] = tc

    return kwargs


# Anthropic stop_reason → OpenAI finish_reason. ``stop_sequence`` is
# treated as a normal completion because the adapter only cares about
# ``tool_calls`` vs "anything else" for control flow.
_STOP_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


def anthropic_message_to_openai(message: Any) -> dict[str, Any]:
    """Project an Anthropic ``Message`` back into OpenAI-shape JSON.

    ``message`` is the object returned by
    ``AsyncAnthropicBedrock.messages.create``. Accepts both the SDK's
    Pydantic object and a raw dict (for test fixtures): we use
    ``getattr`` + dict fallback so either works.
    """

    def _attr(obj: Any, name: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    content_blocks = _attr(message, "content", []) or []
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in content_blocks:
        block_type = _attr(block, "type")
        if block_type == "text":
            text = _attr(block, "text", "") or ""
            if text:
                text_parts.append(str(text))
        elif block_type == "tool_use":
            raw_input = _attr(block, "input", {}) or {}
            if not isinstance(raw_input, str):
                try:
                    arguments = json.dumps(raw_input, ensure_ascii=False)
                except (TypeError, ValueError):
                    arguments = "{}"
            else:
                arguments = raw_input
            tool_calls.append(
                {
                    "id": str(_attr(block, "id", "") or ""),
                    "type": "function",
                    "function": {
                        "name": str(_attr(block, "name", "") or ""),
                        "arguments": arguments,
                    },
                }
            )

    stop_reason = _attr(message, "stop_reason") or "end_turn"
    finish_reason = _STOP_REASON_MAP.get(str(stop_reason), "stop")

    assistant_message: dict[str, Any] = {
        "role": "assistant",
        "content": "\n".join(text_parts) if text_parts else None,
    }
    if tool_calls:
        assistant_message["tool_calls"] = tool_calls

    usage = _attr(message, "usage", None)
    input_tokens = int(_attr(usage, "input_tokens", 0) or 0)
    output_tokens = int(_attr(usage, "output_tokens", 0) or 0)

    return {
        "choices": [
            {
                "index": 0,
                "message": assistant_message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }


async def send_bedrock_chat_completion(
    provider: LlmProviderConfig,
    *,
    payload: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    """Send a single Bedrock ``messages.create`` call.

    Imported lazily so deployments that never use Bedrock don't pay the
    import cost of ``anthropic`` / ``boto3`` at startup.
    """
    from anthropic import AsyncAnthropicBedrock  # type: ignore[import-not-found]

    kwargs = openai_payload_to_anthropic(
        payload, default_model=provider.model
    )
    client = AsyncAnthropicBedrock(
        aws_region=provider.aws_region or None,
        aws_access_key=provider.aws_access_key_id or None,
        aws_secret_key=provider.aws_secret_access_key or None,
        timeout=float(timeout),
    )
    try:
        message = await client.messages.create(**kwargs)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                maybe = close()
                if hasattr(maybe, "__await__"):
                    await maybe  # type: ignore[func-returns-value]
            except Exception:  # pragma: no cover - defensive
                logger.debug("AsyncAnthropicBedrock close failed", exc_info=True)
    return anthropic_message_to_openai(message)
