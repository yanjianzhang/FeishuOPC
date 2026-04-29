"""Context compression for long agent sessions.

Why this module exists
----------------------
``LlmAgentAdapter.execute_with_tools`` keeps a strictly-append ``messages``
list across every tool-call turn. For a TechLead orchestration that goes
``dispatch_role_agent(developer) → read impl-note → dispatch(reviewer)
→ read review → dispatch(bug_fixer) → …`` the prompt token count grows
monotonically and hits the model's context window mid-loop. The observable
failure before this was either:

1. The provider returns ``context_length_exceeded`` and the whole session
   aborts (we lose the entire trajectory);
2. Or we silently waste money sending tens of thousands of stale tokens on
   every turn even when the middle of the conversation is no longer
   relevant.

Hermes Agent solves this with a pluggable ``ContextEngine`` ABC in
``agent/context_engine.py`` plus a default ``context_compressor.py`` that
applies lossy summarization to middle turns. We mirror the same split here
(ABC + default impl) so we can swap in provider-specific strategies later
(e.g. an Anthropic prompt-caching engine that uses cache breakpoints
instead of compressing).

Design decisions
----------------
- **Pluggable ABC, no-op default**: wiring in the adapter asks for a
  compressor; ``NoOpContextCompressor`` makes the feature opt-in per
  deployment (Hermes does the same).
- **Tail window + summarized middle**: we keep the original system prompt
  + the original user message + the last N message entries verbatim, and
  collapse everything in between into one synthetic ``tool`` message
  carrying a short textual summary. That's the same strategy as Hermes's
  default compressor.
- **Tokenization is approximate, not exact**: we purposely don't depend
  on ``tiktoken`` (extra dep + wrong for non-OpenAI providers). Estimation
  uses bytes-per-token heuristics tuned for mixed zh/en Feishu content;
  the 70% trigger ratio gives us enough headroom that approximation
  error doesn't produce false negatives.
- **Summarizer is injected**: compression can either summarize via an
  auxiliary LLM call or fall back to a deterministic truncation message.
  The adapter owns the LLM; the compressor owns the policy. Keeps the
  dependency arrow clean.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Rough bytes-per-token ratios. ASCII-heavy English averages ~4 chars/token
# under cl100k_base; Chinese averages closer to 1.2 chars/token because
# most ideographs are a single token. We split the difference for mixed
# content and bias toward over-estimating (better to compress slightly too
# eagerly than to send an over-window prompt).
#
# These are *approximations*. The real tokenizer varies by provider and
# by model; using tiktoken would be more accurate but would tie us to
# cl100k_base. Our trigger ratio is 0.7 so the safety margin absorbs the
# approximation error — if this assumption ever breaks we'll see it in
# the ``context_length_exceeded`` error rate in audit logs.
_CHARS_PER_TOKEN_MIXED = 2.5


def estimate_tokens(text: str) -> int:
    """Cheap, provider-agnostic token count estimate.

    Good enough for "are we approaching the context window" decisions;
    not good enough for billing reconciliation. Used as input to
    ``should_compress``.
    """
    if not text:
        return 0
    return max(1, int(len(text) / _CHARS_PER_TOKEN_MIXED))


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate tokens across an OpenAI-format messages array.

    Counts role, content, and any serialized tool_calls /
    tool_call_id / name payload. Adds a small per-message overhead
    (~4 tokens) matching OpenAI's own guidance.

    We include every field a chat-completions request actually
    serializes — under-counting tool_call_id and name was a measurable
    miss on long tool loops (each tool response carries both fields and
    on a 30-turn session the delta was >1k tokens).
    """
    total = 0
    for msg in messages:
        total += 4  # role + separator overhead
        content = msg.get("content")
        if isinstance(content, str):
            total += estimate_tokens(content)
        elif content is not None:
            # Tool messages sometimes carry structured content; fall
            # back to a stringified estimate.
            total += estimate_tokens(str(content))
        # Top-level fields on assistant/tool messages that hit the wire:
        # ``tool_call_id`` (tool role), ``name`` (tool / function role).
        if msg.get("tool_call_id"):
            total += estimate_tokens(str(msg["tool_call_id"]))
        if msg.get("name"):
            total += estimate_tokens(str(msg["name"]))
        # tool_calls on assistant messages contribute the id, type,
        # function name, and arguments JSON blob.
        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            if tc.get("id"):
                total += estimate_tokens(str(tc["id"]))
            if tc.get("type"):
                total += estimate_tokens(str(tc["type"]))
            fn = tc.get("function", {})
            total += estimate_tokens(str(fn.get("name", "")))
            total += estimate_tokens(str(fn.get("arguments", "")))
    return total


@dataclass
class CompressionDecision:
    """What the compressor decided to do. Captured so callers can log it."""

    applied: bool
    reason: str
    tokens_before: int = 0
    tokens_after: int = 0
    kept_head: int = 0
    kept_tail: int = 0
    collapsed: int = 0


def _drop_orphan_tool_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove ``role: tool`` entries whose matching assistant call is gone.

    Walks forward; keeps a running set of ``tool_call_id``s emitted by
    the most recent assistant turn. A tool message whose ``tool_call_id``
    isn't in that set is dropped (with a log line so the audit trail
    still shows the compression happened).

    This is a belt to the suspenders added by ``TailWindowCompressor``:
    compression can trim an assistant message whose tool responses land
    in the preserved tail, leaving those tool messages dangling. Most
    providers 400 on dangling tool messages; dropping them is strictly
    safer than forwarding them.
    """
    pending: set[str] = set()
    kept: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            pending = {
                tc.get("id")
                for tc in msg.get("tool_calls") or []
                if isinstance(tc, dict) and tc.get("id")
            }
            kept.append(msg)
            continue
        if role == "tool":
            tcid = msg.get("tool_call_id")
            if not tcid or tcid not in pending:
                logger.info(
                    "context compression dropped orphan tool message "
                    "tool_call_id=%r",
                    tcid,
                )
                continue
            pending.discard(tcid)
            kept.append(msg)
            continue
        # non-assistant, non-tool messages don't clear pending — they
        # interleave with a tool-response stream in some flows.
        kept.append(msg)
    return kept


class ContextCompressor(ABC):
    """Pluggable interface for reshaping ``messages`` before each LLM call.

    Implementations MUST be idempotent — a no-change pass produces the
    input unchanged. The adapter calls ``compress`` defensively before
    every turn; a compressor that can't produce useful work on a given
    input should return ``(messages, CompressionDecision(applied=False))``.
    """

    @abstractmethod
    async def compress(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        task_handle: Any = None,
    ) -> tuple[list[dict[str, Any]], CompressionDecision]:
        """Return possibly-shortened messages + a decision record."""


class NoOpContextCompressor(ContextCompressor):
    """Default: touch nothing.

    Used whenever the deployment hasn't explicitly opted into compression.
    Matches Hermes's "ContextEngine is optional" posture — if you don't
    configure one, you pay the full token bill.
    """

    async def compress(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        task_handle: Any = None,
    ) -> tuple[list[dict[str, Any]], CompressionDecision]:
        return messages, CompressionDecision(
            applied=False, reason="noop_compressor"
        )


# Summarizer signature: given the middle-slice of messages, return a
# short Markdown text summarizing what happened. Returning ``None`` or
# an empty string signals "please truncate instead" — the compressor
# falls back to a deterministic placeholder rather than failing.
SummarizerCallable = Callable[
    [list[dict[str, Any]]], Awaitable[Optional[str]]
]


class TailWindowCompressor(ContextCompressor):
    """Keep system prompt + user query + last ``keep_tail_turns``; summarize the rest.

    Triggers when estimated prompt tokens exceed
    ``trigger_ratio * max_context_tokens``. Below the threshold it is a
    no-op, so it's safe to install everywhere.

    Invariants the reducer guarantees:

    - ``messages[0]`` (the ``system`` message) is always preserved.
    - The first ``user`` message found is preserved (this is the original
      task description; without it the LLM loses its north star).
    - The last ``keep_tail_turns`` messages are preserved verbatim (recent
      tool outputs and assistant reasoning are what drive the next turn).
    - The "middle" (everything else) is collapsed into a single synthetic
      ``tool`` message whose content is either the auxiliary summary or a
      deterministic truncation notice.

    Why synthetic ``user`` and not ``tool``? The OpenAI chat schema
    (and every OpenAI-compatible provider) requires every ``role: tool``
    message to be preceded by an ``assistant`` message whose
    ``tool_calls[].id`` equals the tool message's ``tool_call_id``. A
    floating ``role: tool`` injected into the middle of a rebuilt
    message list fails that contract and triggers a 400 on every
    request. We sidestep that entirely by emitting the summary as a
    ``role: user`` message with a ``[context_compression]`` prefix —
    all providers accept user messages anywhere; the prefix keeps the
    LLM from mistaking the summary for a new user instruction.
    """

    # Prefix is recognized by the LLM as "this is a system-injected
    # summary, not a new task". Kept short to reduce tokens and match
    # Hermes's convention.
    COMPRESSION_PREFIX = "[context_compression]"

    def __init__(
        self,
        *,
        max_context_tokens: int = 32_000,
        trigger_ratio: float = 0.7,
        keep_tail_turns: int = 6,
        summarizer: SummarizerCallable | None = None,
        hard_min_messages: int = 4,
    ) -> None:
        if not 0.1 <= trigger_ratio <= 0.95:
            raise ValueError("trigger_ratio must be within [0.1, 0.95]")
        if keep_tail_turns < 1:
            raise ValueError("keep_tail_turns must be >= 1")
        if hard_min_messages < 2:
            raise ValueError("hard_min_messages must be >= 2")

        self.max_context_tokens = max_context_tokens
        self.trigger_ratio = trigger_ratio
        self.keep_tail_turns = keep_tail_turns
        self._summarizer = summarizer
        # Below this total message count we refuse to compress, even if
        # tokens overflow — the compressed form (system + user + summary
        # + a tail of 1–2) is not meaningfully shorter than the original
        # and can strip context the LLM still needs.
        self._hard_min_messages = hard_min_messages

    async def compress(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        task_handle: Any = None,
    ) -> tuple[list[dict[str, Any]], CompressionDecision]:
        if len(messages) < self._hard_min_messages:
            return messages, CompressionDecision(
                applied=False,
                reason="below_hard_min_messages",
                tokens_before=estimate_messages_tokens(messages),
            )

        tokens_before = estimate_messages_tokens(messages)
        threshold = int(self.max_context_tokens * self.trigger_ratio)
        if tokens_before < threshold:
            return messages, CompressionDecision(
                applied=False,
                reason="below_threshold",
                tokens_before=tokens_before,
            )

        # Locate head (system + first user) and tail (last N).
        head_indices: list[int] = []
        if messages and messages[0].get("role") == "system":
            head_indices.append(0)
        for i, m in enumerate(messages):
            if m.get("role") == "user":
                head_indices.append(i)
                break

        n = len(messages)
        tail_start = max(n - self.keep_tail_turns, max(head_indices) + 1 if head_indices else 0)
        middle_start = (max(head_indices) + 1) if head_indices else 0
        middle_end = tail_start

        if middle_end - middle_start <= 0:
            # Nothing meaningful in the middle; compression would be a
            # no-op shuffle. Report this so audit logs explain why we
            # paid the full token bill.
            return messages, CompressionDecision(
                applied=False,
                reason="no_middle_to_compress",
                tokens_before=tokens_before,
            )

        middle = messages[middle_start:middle_end]
        summary_text = await self._render_summary(middle, task_handle=task_handle)

        new_messages: list[dict[str, Any]] = [messages[i] for i in head_indices]
        # Synthetic injection is a ``user`` message, not ``tool`` —
        # see class docstring for why. The prefix is the signal to the
        # LLM that this isn't a human instruction.
        new_messages.append(
            {
                "role": "user",
                "content": f"{self.COMPRESSION_PREFIX} {summary_text}",
            }
        )
        new_messages.extend(messages[tail_start:])
        # Defensive: if the tail starts mid-tool-call (an assistant
        # message with pending tool_calls) we keep it intact. If the
        # VERY FIRST tail message is a ``role: tool`` response whose
        # matching ``tool_calls`` assistant was dropped by the head
        # window, that message becomes an orphan and providers will
        # 400. Strip such orphans — the summary already explains what
        # happened.
        new_messages = _drop_orphan_tool_messages(new_messages)

        tokens_after = estimate_messages_tokens(new_messages)
        logger.info(
            "context compression applied tokens_before=%d tokens_after=%d "
            "kept_head=%d kept_tail=%d collapsed=%d",
            tokens_before,
            tokens_after,
            len(head_indices),
            n - tail_start,
            middle_end - middle_start,
        )
        return new_messages, CompressionDecision(
            applied=True,
            reason="tail_window_summarized",
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            kept_head=len(head_indices),
            kept_tail=n - tail_start,
            collapsed=middle_end - middle_start,
        )

    async def _render_summary(
        self, middle: list[dict[str, Any]], *, task_handle: Any = None
    ) -> str:
        """Produce a text summary of the collapsed middle turns.

        Falls back to a deterministic placeholder if:
        - no summarizer is configured (opt-in via ``summarizer`` arg), or
        - the summarizer raised / returned empty.

        Deterministic fallback intentionally names each tool that was
        called so the LLM can reconstruct the *shape* of what happened
        even without the full content.
        """
        if self._summarizer is not None:
            try:
                summary = await self._summarizer(middle)
                if summary:
                    return f"[Context compression] {summary.strip()}"
            except Exception:  # pragma: no cover — safety net
                logger.exception(
                    "context compression summarizer raised; using fallback"
                )

        session_summary_text = self._build_session_summary_text(task_handle)
        deterministic = self._build_deterministic_summary(middle)
        if session_summary_text:
            return (
                "[Context compression] Thread summary before truncation: "
                f"{session_summary_text}\n{deterministic}"
            )
        return deterministic

    @staticmethod
    def _build_deterministic_summary(middle: list[dict[str, Any]]) -> str:
        """Build a rule-based outline of the middle turns.

        Lists tool calls and assistant intermediate replies in order so
        the downstream LLM still has a breadcrumb trail. This is the
        lossless-ish path; real summarization is better but this is the
        worst case.
        """
        lines = [
            f"[Context compression] The following {len(middle)} "
            "intermediate turns were collapsed to stay within the context "
            "window. Full content is no longer available; outline follows."
        ]
        for i, m in enumerate(middle, start=1):
            role = m.get("role", "?")
            tool_calls = m.get("tool_calls") or []
            if tool_calls:
                names = [
                    (tc.get("function") or {}).get("name", "?")
                    for tc in tool_calls
                    if isinstance(tc, dict)
                ]
                lines.append(
                    f"  {i}. {role} called tools: "
                    + ", ".join(names)
                )
            elif role == "tool":
                content = str(m.get("content") or "")
                preview = content[:120].replace("\n", " ")
                lines.append(f"  {i}. tool result: {preview}…")
            else:
                content = str(m.get("content") or "")
                preview = content[:120].replace("\n", " ")
                lines.append(f"  {i}. {role}: {preview}…")
        return "\n".join(lines)

    @staticmethod
    def _build_session_summary_text(task_handle: Any) -> str:
        if task_handle is None:
            return ""
        try:
            from feishu_agent.team.session_summary_service import (
                SessionSummaryService,
            )

            summary = SessionSummaryService().build_for_handle(task_handle)
        except Exception:  # pragma: no cover - defensive
            logger.debug(
                "failed to build session summary for compression", exc_info=True
            )
            return ""
        return summary.to_compression_text()
