"""`OutputEvent` data model (spec 005 §3.1, M1-A / T531).

`OutputEvent` is the composer's internal currency — it sits one layer
up from raw `hook_bus` events (which are heterogeneous — `on_tool_call`
payloads differ from `post_llm_call` payloads) and one layer below
`MessageEnvelope` (which is already committed to a `MessageKind` +
card layout).

The translator (`hook_translator.py`, M1-C) converts bus events to
`OutputEvent`; the composer (`composer.py`, M1-E) drains the buffer,
groups via `fold_policy`, and hands groups to leaf formatters.

This module is intentionally pure: no imports from anything in
``feishu_agent`` other than stdlib, so it can be consumed by tests and
downstream specs (006 references `OutputEvent.trace_id` format) with
zero risk of cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

EventKind = Literal[
    "tool_use_started",
    "tool_use_finished",
    "thinking_chunk",
    "plan_proposed",
    "progress_update",
    "pending_action",
    "handoff_request",
    "rate_limited",
    "final_answer",
    "error",
]

#: Kinds that force an immediate flush (bypass the 500ms flush interval).
#: Per spec 005 §4.1: ``composer.emit`` checks membership to decide whether
#: to schedule a ``flush(force=True)`` task right away. Keep this set small —
#: every kind listed here is either user-facing-urgent (needs response
#: within seconds) or non-foldable (grouping more would only delay).
IMMEDIATE_KINDS: frozenset[EventKind] = frozenset({
    "pending_action",
    "plan_proposed",
    "handoff_request",
    "rate_limited",
    "error",
})


@dataclass(frozen=True)
class OutputEvent:
    """One structured event emitted during a role's execution.

    Frozen because events flow through an ``asyncio.Queue`` into the
    fold policy and then into leaf formatters; mutation anywhere on the
    path would desync sequence / idempotency accounting.
    """

    kind: EventKind
    trace_id: str
    role: str
    seq: int
    ts_ms: int
    payload: dict[str, Any] = field(default_factory=dict)
    fold_key: str | None = None
    inflight: bool = False

    @property
    def is_immediate(self) -> bool:
        """Convenience for composer / tests: does this kind skip batching?"""
        return self.kind in IMMEDIATE_KINDS
