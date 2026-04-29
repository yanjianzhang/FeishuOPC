"""Unit tests for context_compression.

Focus:
- Threshold gating: below trigger → no-op; above → compresses.
- Invariants: system + first user + tail preserved; middle collapsed.
- Summarizer wiring: async callable used when provided, fallback used
  when it returns empty or raises.
- Token estimator is within an order of magnitude of the obvious
  truth (we don't test exact counts — it's deliberately approximate).
"""

from __future__ import annotations

import pytest

from feishu_agent.core.context_compression import (
    NoOpContextCompressor,
    TailWindowCompressor,
    estimate_messages_tokens,
    estimate_tokens,
)
from feishu_agent.team.task_event_log import TaskKey
from feishu_agent.team.task_service import TaskService


@pytest.mark.asyncio
async def test_noop_compressor_returns_input_unchanged():
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]
    result, decision = await NoOpContextCompressor().compress(messages, model="m")
    assert result is messages  # identity — no copy expected
    assert decision.applied is False


def test_estimate_tokens_scales_roughly_with_length():
    assert estimate_tokens("") == 0
    short = estimate_tokens("hi")
    long = estimate_tokens("hi " * 1000)
    # Not a tight bound — just that longer content → more tokens.
    assert long > short * 50


def test_estimate_messages_counts_role_overhead_and_content():
    messages = [
        {"role": "system", "content": "abc"},
        {"role": "user", "content": "defg"},
    ]
    # Overhead (4 per msg × 2 = 8) + content-derived tokens.
    count = estimate_messages_tokens(messages)
    assert count > 8


@pytest.mark.asyncio
async def test_tail_window_below_threshold_is_noop():
    compressor = TailWindowCompressor(
        max_context_tokens=100_000, trigger_ratio=0.9, keep_tail_turns=2
    )
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "u2"},
    ]
    result, decision = await compressor.compress(messages, model="m")
    assert result == messages
    assert decision.applied is False
    assert decision.reason == "below_threshold"


@pytest.mark.asyncio
async def test_tail_window_above_threshold_collapses_middle():
    # Tiny threshold to force compression regardless of content size.
    compressor = TailWindowCompressor(
        max_context_tokens=50, trigger_ratio=0.1, keep_tail_turns=2
    )
    # The tail ends with a valid assistant+tool_calls ↔ tool_response
    # pair, which is what a real mid-flight session looks like. Any
    # schema-incorrect input (lone ``role: tool`` messages) would be
    # silently dropped by the orphan stripper in _drop_orphan_tool_messages.
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "original task"},
        {"role": "assistant", "content": "middle 1"},
        {"role": "assistant", "content": "middle 2"},
        {"role": "assistant", "content": "middle 3"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_y",
                    "type": "function",
                    "function": {"name": "foo", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_y", "content": "tail 2"},
    ]
    result, decision = await compressor.compress(messages, model="m")
    assert decision.applied is True
    assert decision.collapsed == 3
    # system and original user preserved
    assert result[0]["content"] == "system prompt"
    assert result[1]["content"] == "original task"
    # Synthetic summary follows the head. Use `role: user` with a
    # ``[context_compression]`` prefix — see B-2 in the tier-1 review.
    assert result[2]["role"] == "user"
    assert result[2]["content"].startswith(
        TailWindowCompressor.COMPRESSION_PREFIX
    )
    assert "Context compression" in result[2]["content"]
    # Tail pair preserved verbatim and the tool response is still
    # anchored to its preceding assistant tool_calls entry.
    assert result[-2]["role"] == "assistant"
    assert result[-2]["tool_calls"][0]["id"] == "call_y"
    assert result[-1]["role"] == "tool"
    assert result[-1]["tool_call_id"] == "call_y"
    assert result[-1]["content"] == "tail 2"


@pytest.mark.asyncio
async def test_tail_window_drops_orphan_tool_message():
    """A ``role: tool`` whose matching assistant got compressed away is
    stripped before we send to the provider. Without this, providers
    return 400 ``tool messages must follow a matching tool_calls``."""
    compressor = TailWindowCompressor(
        max_context_tokens=50, trigger_ratio=0.1, keep_tail_turns=1
    )
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        # This assistant+tool pair would be torn in half: the assistant
        # lands in the middle (compressed), the tool response lands in
        # the tail (kept) but then has no anchor.
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_orphan",
                    "type": "function",
                    "function": {"name": "foo", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_orphan", "content": "orphan"},
    ]
    result, decision = await compressor.compress(messages, model="m")
    assert decision.applied is True
    # No role-tool message should survive — the stripper removed it.
    assert all(m.get("role") != "tool" for m in result)


@pytest.mark.asyncio
async def test_tail_window_uses_injected_summarizer():
    calls: list[list[dict]] = []

    async def fake_summarizer(middle):
        calls.append(list(middle))
        return "fake summary text"

    compressor = TailWindowCompressor(
        max_context_tokens=50,
        trigger_ratio=0.1,
        keep_tail_turns=1,
        summarizer=fake_summarizer,
    )
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "mid1"},
        {"role": "assistant", "content": "mid2"},
        {"role": "assistant", "content": "tail"},
    ]
    result, decision = await compressor.compress(messages, model="m")
    assert decision.applied is True
    assert calls and len(calls[0]) == 2  # middle had 2 items
    assert result[2]["role"] == "user"
    assert "fake summary text" in result[2]["content"]
    assert result[2]["content"].startswith(
        TailWindowCompressor.COMPRESSION_PREFIX
    )


@pytest.mark.asyncio
async def test_tail_window_falls_back_on_summarizer_exception():
    async def blowing(_middle):
        raise RuntimeError("summarizer broken")

    compressor = TailWindowCompressor(
        max_context_tokens=50,
        trigger_ratio=0.1,
        keep_tail_turns=1,
        summarizer=blowing,
    )
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "mid1"},
        {"role": "assistant", "content": "tail"},
    ]
    result, decision = await compressor.compress(messages, model="m")
    # Compression still applies; fallback summary is deterministic.
    assert decision.applied is True
    assert result[2]["role"] == "user"
    assert "Context compression" in result[2]["content"]


@pytest.mark.asyncio
async def test_tail_window_uses_session_summary_when_task_handle_available(tmp_path):
    task_service = TaskService(tasks_root=tmp_path)
    handle = task_service.open_or_resume(
        TaskKey(bot_name="bot", chat_id="c1", root_id="r1"),
        role_name="tech_lead",
    )
    handle.append(
        kind="message.inbound",
        payload={"command_text": "继续修 reviewer 报的 lint"},
    )
    handle.append(kind="state.mode_set", payload={"mode": "plan"})
    handle.append(
        kind="state.todo_added",
        payload={"id": "todo-1", "text": "修 lint", "status": "open"},
    )
    compressor = TailWindowCompressor(
        max_context_tokens=50,
        trigger_ratio=0.1,
        keep_tail_turns=1,
    )
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "middle 1"},
        {"role": "assistant", "content": "tail"},
    ]
    result, decision = await compressor.compress(
        messages, model="m", task_handle=handle
    )
    assert decision.applied is True
    assert "Thread summary before truncation" in result[2]["content"]
    assert "Open loops" in result[2]["content"]


@pytest.mark.asyncio
async def test_below_hard_min_messages_skips_even_when_over_threshold():
    compressor = TailWindowCompressor(
        max_context_tokens=50, trigger_ratio=0.1, keep_tail_turns=1, hard_min_messages=10
    )
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "x" * 500},
    ]
    result, decision = await compressor.compress(messages, model="m")
    assert decision.applied is False
    assert decision.reason == "below_hard_min_messages"
    assert result == messages


def test_tail_window_validates_constructor_args():
    with pytest.raises(ValueError):
        TailWindowCompressor(max_context_tokens=100, trigger_ratio=1.5, keep_tail_turns=1)
    with pytest.raises(ValueError):
        TailWindowCompressor(max_context_tokens=100, trigger_ratio=0.5, keep_tail_turns=0)
    with pytest.raises(ValueError):
        TailWindowCompressor(max_context_tokens=100, trigger_ratio=0.5, keep_tail_turns=1, hard_min_messages=1)
