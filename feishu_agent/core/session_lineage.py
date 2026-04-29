"""Session lineage tracking for nested agent dispatches.

Problem
-------
``dispatch_role_agent`` routinely spawns 3–4 levels deep:

    tech_lead → reviewer → bug_fixer → reviewer (2nd pass) → …

Each call gets a fresh ``trace_id`` so audit JSONs don't overwrite
each other. But when something goes wrong, there's no way to see
"this reviewer run was the kid of that tech_lead run" — the audit
dir is flat, the Feishu thread just shows individual tool calls.

Goal
----
Record ``parent_trace_id`` on every spawned sub-agent so we can:

1. Reconstruct the spawn tree from audit logs (post-hoc).
2. Render lineage breadcrumbs in Feishu thread updates
   ("tech_lead#abc → reviewer#def → bug_fixer#ghi").
3. Propagate cancel requests down the tree (the cancel token's job).

Design
------
- ``LineageNode`` stores ``trace_id`` + ``parent_trace_id`` + ``role``
  + timestamps + final outcome. One node per agent session.
- ``SessionLineageTracker`` is the registry. Nodes are added on
  spawn, closed on session end, and persisted to the audit log so
  an off-process tool can reconstruct the tree.
- Integration is a hook subscription: ``on_sub_agent_spawn`` adds a
  child node, ``on_sub_agent_end`` closes it, ``on_session_end``
  closes the root. This keeps the lineage bookkeeping decoupled
  from the executor code.

Why not inherit trace_id from the parent?
-----------------------------------------
We'd lose per-session audit JSONs (``{trace_id}.json`` would
overwrite). Keeping a distinct trace_id per node + a parent pointer
is the git commit graph pattern: cheap, unambiguous, and lets us
query "all descendants of X" trivially.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from time import time
from typing import Iterable

from feishu_agent.core.hook_bus import HookBus

logger = logging.getLogger(__name__)


@dataclass
class LineageNode:
    """One agent session in the lineage tree.

    ``parent_trace_id`` is ``None`` for root sessions (the TL that
    Feishu delivered the user message to). For every other node it's
    the ``trace_id`` of the direct parent.

    ``ended_at`` / ``ok`` are set when the session concludes; while a
    session is running they stay at their defaults. A node with
    ``ended_at is None`` is "in flight" — useful for detecting stuck
    sessions in monitoring.
    """

    trace_id: str
    parent_trace_id: str | None
    role: str
    started_at: float = field(default_factory=time)
    ended_at: float | None = None
    ok: bool | None = None
    stop_reason: str | None = None

    @property
    def duration_ms(self) -> int | None:
        if self.ended_at is None:
            return None
        return int((self.ended_at - self.started_at) * 1000)


class SessionLineageTracker:
    """In-memory registry of lineage nodes for one Feishu message.

    Lifecycle matches a request:
    - Created with the root ``trace_id`` / ``role`` at message ingress.
    - ``spawn_child`` called (by a hook subscriber) for each sub-agent.
    - ``close`` called (by another hook subscriber) when each session
      ends.
    - Dropped when the request returns — no cross-request persistence;
      the audit log is the durable record.

    Thread-safety: guarded by a mutex because ``HookBus.afire`` may
    run from multiple concurrent sub-agent loops in principle (we
    don't today, but we might). The mutex is cheap and uncontended.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, LineageNode] = {}
        self._lock = threading.Lock()

    # --- mutation ---------------------------------------------------------

    def record_root(self, trace_id: str, role: str) -> LineageNode:
        """Register the root-level session.

        Called once per Feishu message, before any sub-agents spawn.
        Idempotent on the same trace_id — re-registration is treated
        as a no-op so tests that replay events don't double-count.
        """
        with self._lock:
            if trace_id in self._nodes:
                return self._nodes[trace_id]
            node = LineageNode(
                trace_id=trace_id, parent_trace_id=None, role=role
            )
            self._nodes[trace_id] = node
            return node

    def spawn_child(
        self, *, parent_trace_id: str, child_trace_id: str, role: str
    ) -> LineageNode:
        """Register a child node.

        If the parent isn't registered (e.g., an orphaned sub-agent
        spawned outside the normal flow), we still record the child
        with ``parent_trace_id`` set — the lineage just won't reach
        back to a root. We log a warning so orphans are visible.
        """
        with self._lock:
            if parent_trace_id not in self._nodes:
                logger.warning(
                    "lineage spawn_child: parent %s not registered; "
                    "child %s will be an orphan",
                    parent_trace_id,
                    child_trace_id,
                )
            node = LineageNode(
                trace_id=child_trace_id,
                parent_trace_id=parent_trace_id,
                role=role,
            )
            self._nodes[child_trace_id] = node
            return node

    def close(
        self,
        trace_id: str,
        *,
        ok: bool,
        stop_reason: str | None = None,
    ) -> LineageNode | None:
        """Mark a session as ended. No-op for unknown trace_ids."""
        with self._lock:
            node = self._nodes.get(trace_id)
            if node is None:
                return None
            node.ended_at = time()
            node.ok = ok
            node.stop_reason = stop_reason
            return node

    # --- queries ----------------------------------------------------------

    def get(self, trace_id: str) -> LineageNode | None:
        with self._lock:
            return self._nodes.get(trace_id)

    def ancestors(self, trace_id: str) -> list[LineageNode]:
        """Return the ancestor chain from root → direct parent.

        Root-first ordering matches how we want to render breadcrumbs
        ("root → child → grandchild") for human readers.

        Guards against cycles by tracking visited trace_ids — cycles
        shouldn't happen with our generation strategy but a defensive
        check costs nothing and prevents an infinite loop if someone
        misuses the API.
        """
        chain: list[LineageNode] = []
        seen: set[str] = set()
        with self._lock:
            node = self._nodes.get(trace_id)
            while node and node.parent_trace_id:
                if node.parent_trace_id in seen:
                    logger.warning(
                        "lineage cycle detected at trace_id=%s",
                        node.parent_trace_id,
                    )
                    break
                seen.add(node.parent_trace_id)
                parent = self._nodes.get(node.parent_trace_id)
                if parent is None:
                    break
                chain.append(parent)
                node = parent
        chain.reverse()
        return chain

    def all_nodes(self) -> list[LineageNode]:
        """Snapshot of every node. For audit persistence."""
        with self._lock:
            return list(self._nodes.values())

    def descendants(self, trace_id: str) -> list[LineageNode]:
        """Return every node that descends from ``trace_id``.

        Used when we want to cancel an entire subtree or render a
        partial spawn tree. Not depth-ordered — the caller can sort
        by ``started_at`` if they need stable ordering.
        """
        with self._lock:
            # Build a parent → children index once; O(N) per call is
            # fine at our scale (~dozen nodes per request).
            children: dict[str, list[LineageNode]] = {}
            for node in self._nodes.values():
                if node.parent_trace_id:
                    children.setdefault(node.parent_trace_id, []).append(node)

        out: list[LineageNode] = []
        stack: list[str] = [trace_id]
        while stack:
            tid = stack.pop()
            for child in children.get(tid, ()):
                out.append(child)
                stack.append(child.trace_id)
        return out

    # --- rendering --------------------------------------------------------

    def render_breadcrumb(self, trace_id: str) -> str:
        """Render the root → ... → trace_id chain as a short string.

        Example output: ``tech_lead#abc12345 → reviewer#def01234``.
        Short hashes (first 8 chars) keep Feishu lines readable.
        """
        chain = self.ancestors(trace_id) + [n for n in [self.get(trace_id)] if n]
        parts = [f"{n.role}#{n.trace_id[:8]}" for n in chain]
        return " → ".join(parts) if parts else f"#{trace_id[:8]}"

    def render_tree(self) -> str:
        """ASCII tree of the whole lineage.

        Handy for audit log payloads ("here's what the run looked
        like") and for debug thread updates. Root nodes (parent=None)
        are listed first; children indent by two spaces per depth.
        """
        nodes = self.all_nodes()
        by_parent: dict[str | None, list[LineageNode]] = {}
        for n in nodes:
            by_parent.setdefault(n.parent_trace_id, []).append(n)

        lines: list[str] = []

        def _walk(parent: str | None, depth: int) -> None:
            children = sorted(
                by_parent.get(parent, ()), key=lambda c: c.started_at
            )
            for child in children:
                ok_marker = (
                    "✓"
                    if child.ok is True
                    else ("✗" if child.ok is False else "⋯")
                )
                dur = (
                    f" ({child.duration_ms}ms)"
                    if child.duration_ms is not None
                    else ""
                )
                lines.append(
                    f"{'  ' * depth}{ok_marker} {child.role}#{child.trace_id[:8]}{dur}"
                )
                _walk(child.trace_id, depth + 1)

        _walk(None, 0)
        return "\n".join(lines) if lines else "(empty)"

    # --- hook integration -------------------------------------------------

    def attach_to(self, bus: HookBus, *, root_trace_id: str, root_role: str) -> None:
        """Wire this tracker to a ``HookBus`` lifecycle.

        Subscribes to ``on_sub_agent_spawn`` / ``on_sub_agent_end`` /
        ``on_session_end`` so lineage bookkeeping is fully automatic
        once wired. Call once per request at the point you first have
        the root ``trace_id``.
        """
        self.record_root(root_trace_id, root_role)

        def _on_spawn(event: str, payload: dict[str, object]) -> None:
            parent = str(payload.get("parent_trace_id") or "")
            child = str(payload.get("child_trace_id") or "")
            role = str(payload.get("role") or "unknown")
            if parent and child:
                self.spawn_child(
                    parent_trace_id=parent,
                    child_trace_id=child,
                    role=role,
                )

        def _on_end(event: str, payload: dict[str, object]) -> None:
            # ``on_sub_agent_end`` uses ``child_trace_id``; plain
            # ``on_session_end`` (emitted by the tool loop) uses
            # ``trace_id``. Accept either to stay future-proof.
            trace = str(
                payload.get("child_trace_id") or payload.get("trace_id") or ""
            )
            if not trace:
                return
            self.close(
                trace,
                ok=bool(payload.get("ok", False)),
                stop_reason=(
                    str(payload["stop_reason"])
                    if payload.get("stop_reason") is not None
                    else None
                ),
            )

        bus.subscribe("on_sub_agent_spawn", _on_spawn)
        bus.subscribe("on_sub_agent_end", _on_end)
        bus.subscribe("on_session_end", _on_end)


def render_breadcrumb_from_nodes(nodes: Iterable[LineageNode]) -> str:
    """Purely-functional breadcrumb rendering for external callers.

    Mirrors ``SessionLineageTracker.render_breadcrumb`` but takes an
    ordered iterable so tests and audit tooling can use the format
    without instantiating the tracker.
    """
    return " → ".join(f"{n.role}#{n.trace_id[:8]}" for n in nodes)
