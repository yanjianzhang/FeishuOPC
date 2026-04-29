"""Tests for ``SessionLineageTracker``.

Coverage focuses on the three things the tracker has to get right:

1. Ancestors / descendants queries must handle roots, single-parent,
   and multi-level chains.
2. ``attach_to`` correctly subscribes to the HookBus and closes nodes
   on end events.
3. Orphans (child spawned without a registered parent) don't explode
   — we want the tracker to keep working when hooks get mis-wired.
"""

from __future__ import annotations

import pytest

from feishu_agent.core.hook_bus import HookBus
from feishu_agent.core.session_lineage import (
    LineageNode,
    SessionLineageTracker,
    render_breadcrumb_from_nodes,
)


def test_record_root_is_idempotent():
    t = SessionLineageTracker()
    n1 = t.record_root("abc", "tech_lead")
    n2 = t.record_root("abc", "tech_lead")
    assert n1 is n2


def test_spawn_child_links_to_parent():
    t = SessionLineageTracker()
    t.record_root("root", "tech_lead")
    child = t.spawn_child(
        parent_trace_id="root", child_trace_id="c1", role="reviewer"
    )
    assert child.parent_trace_id == "root"
    ancestors = t.ancestors("c1")
    assert [n.trace_id for n in ancestors] == ["root"]


def test_ancestors_returns_root_first():
    t = SessionLineageTracker()
    t.record_root("r", "tech_lead")
    t.spawn_child(parent_trace_id="r", child_trace_id="c1", role="reviewer")
    t.spawn_child(parent_trace_id="c1", child_trace_id="c2", role="bug_fixer")

    ancestors = t.ancestors("c2")
    assert [n.trace_id for n in ancestors] == ["r", "c1"]


def test_descendants_finds_whole_subtree():
    t = SessionLineageTracker()
    t.record_root("r", "tech_lead")
    t.spawn_child(parent_trace_id="r", child_trace_id="a", role="dev")
    t.spawn_child(parent_trace_id="a", child_trace_id="a1", role="rev")
    t.spawn_child(parent_trace_id="r", child_trace_id="b", role="inspect")

    desc_ids = {n.trace_id for n in t.descendants("r")}
    assert desc_ids == {"a", "a1", "b"}

    assert {n.trace_id for n in t.descendants("a")} == {"a1"}


def test_orphan_child_is_recorded_not_raised(caplog):
    """Spawning a child with an unknown parent must NOT raise. We log
    a warning and keep going — the tree will be partially rooted, but
    the rest of the session still has lineage."""
    t = SessionLineageTracker()
    with caplog.at_level("WARNING"):
        t.spawn_child(
            parent_trace_id="ghost", child_trace_id="c1", role="reviewer"
        )
    assert t.get("c1") is not None
    assert any("orphan" in rec.message for rec in caplog.records)


def test_close_sets_duration_and_ok():
    t = SessionLineageTracker()
    t.record_root("r", "tech_lead")
    node = t.get("r")
    assert node is not None and node.duration_ms is None
    t.close("r", ok=True, stop_reason="complete")
    node = t.get("r")
    assert node is not None
    assert node.ok is True
    assert node.stop_reason == "complete"
    assert node.duration_ms is not None
    assert node.duration_ms >= 0


def test_close_unknown_trace_is_noop():
    t = SessionLineageTracker()
    assert t.close("nope", ok=True) is None


def test_render_breadcrumb_uses_short_hash():
    t = SessionLineageTracker()
    t.record_root("abc12345678", "tech_lead")
    t.spawn_child(
        parent_trace_id="abc12345678",
        child_trace_id="def01234567",
        role="reviewer",
    )
    bc = t.render_breadcrumb("def01234567")
    assert bc == "tech_lead#abc12345 → reviewer#def01234"


def test_render_tree_shows_status_markers():
    t = SessionLineageTracker()
    t.record_root("r", "tech_lead")
    t.spawn_child(parent_trace_id="r", child_trace_id="c1", role="reviewer")
    t.spawn_child(parent_trace_id="r", child_trace_id="c2", role="inspect")
    t.close("c1", ok=True, stop_reason="complete")
    t.close("c2", ok=False, stop_reason="error")

    tree = t.render_tree()
    assert "tech_lead#r" in tree
    assert "✓ reviewer#c1" in tree  # success marker
    assert "✗ inspect#c2" in tree   # failure marker


@pytest.mark.asyncio
async def test_attach_to_wires_spawn_and_end_handlers():
    """The bus subscription flow — what production actually uses."""
    bus = HookBus()
    tracker = SessionLineageTracker()
    tracker.attach_to(bus, root_trace_id="root", root_role="tech_lead")

    await bus.afire(
        "on_sub_agent_spawn",
        {
            "parent_trace_id": "root",
            "child_trace_id": "c1",
            "role": "reviewer",
        },
    )
    await bus.afire(
        "on_sub_agent_end",
        {
            "parent_trace_id": "root",
            "child_trace_id": "c1",
            "role": "reviewer",
            "ok": True,
            "stop_reason": "complete",
        },
    )

    node = tracker.get("c1")
    assert node is not None
    assert node.parent_trace_id == "root"
    assert node.ok is True


@pytest.mark.asyncio
async def test_attach_to_accepts_session_end_for_root_close():
    """``on_session_end`` from the tool loop uses ``trace_id`` (not
    ``child_trace_id``). Lineage should still close the node."""
    bus = HookBus()
    tracker = SessionLineageTracker()
    tracker.attach_to(bus, root_trace_id="root", root_role="tech_lead")

    await bus.afire(
        "on_session_end",
        {"trace_id": "root", "ok": False, "stop_reason": "error"},
    )

    root = tracker.get("root")
    assert root is not None
    assert root.ok is False


def test_render_breadcrumb_from_nodes_pure():
    """Pure-function variant for use outside the tracker."""
    nodes = [
        LineageNode(trace_id="abc12345", parent_trace_id=None, role="tl"),
        LineageNode(trace_id="def98765", parent_trace_id="abc12345", role="rev"),
    ]
    assert render_breadcrumb_from_nodes(nodes) == "tl#abc12345 → rev#def98765"
