"""Unit tests for ``feishu_agent.presentation.events`` (T535).

These pin the two contracts that 006 and future hook producers lean on:
* the closed ``EventKind`` literal set
* the ``IMMEDIATE_KINDS`` subset (which governs composer latency)
"""

from __future__ import annotations

import pytest

from feishu_agent.presentation.events import (
    IMMEDIATE_KINDS,
    OutputEvent,
)

ALL_EVENT_KINDS = {
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
}


def test_immediate_kinds_is_exact_five():
    """Immediate flush kinds are a stability contract.

    If this set grows, every composer latency test plus the rollout
    doc has to be re-justified — so we pin the exact membership.
    """
    assert IMMEDIATE_KINDS == frozenset({
        "pending_action",
        "plan_proposed",
        "handoff_request",
        "rate_limited",
        "error",
    })


def test_immediate_kinds_is_frozenset():
    """Callers must not be able to mutate the set at runtime."""
    assert isinstance(IMMEDIATE_KINDS, frozenset)


def test_immediate_kinds_subset_of_all_kinds():
    assert IMMEDIATE_KINDS <= ALL_EVENT_KINDS


def test_output_event_is_frozen():
    evt = OutputEvent(
        kind="tool_use_started",
        trace_id="t",
        role="tech_lead",
        seq=1,
        ts_ms=0,
    )
    with pytest.raises(Exception):  # FrozenInstanceError on 3.11+, AttributeError on 3.10
        evt.seq = 2  # type: ignore[misc]


def test_output_event_defaults():
    evt = OutputEvent(
        kind="tool_use_started",
        trace_id="t",
        role="tech_lead",
        seq=1,
        ts_ms=0,
    )
    assert evt.payload == {}
    assert evt.fold_key is None
    assert evt.inflight is False


def test_is_immediate_reflects_immediate_kinds():
    immediate = OutputEvent(kind="pending_action", trace_id="t", role="r", seq=1, ts_ms=0)
    foldable = OutputEvent(kind="tool_use_started", trace_id="t", role="r", seq=2, ts_ms=0)
    assert immediate.is_immediate is True
    assert foldable.is_immediate is False


def test_output_event_payload_isolated_per_instance():
    """Default-factory dicts must not be shared between instances."""
    a = OutputEvent(kind="error", trace_id="t", role="r", seq=1, ts_ms=0)
    b = OutputEvent(kind="error", trace_id="t", role="r", seq=2, ts_ms=0)
    a.payload["x"] = 1
    assert "x" not in b.payload
