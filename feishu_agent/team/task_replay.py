"""Fold ``TaskEvent`` streams into a snapshot-friendly view.

Why this is a separate module
------------------------------
``TaskEventLog`` stores raw events; it is deliberately ignorant of the
*semantics* of ``kind``. M2 will introduce a richer :class:`TaskState`
typed over ``mode`` / ``plan`` / ``todos`` / ``tool_health`` and a full
projector; for M1 we only need enough projection to:

1. Drive the ``task_inspect`` CLI (who said what, which tools ran,
   did we compress, where did the last message leave off).
2. Write a ``state.json`` snapshot that ``TaskService.open_or_resume``
   can read on resume to skip early replay. We keep the shape small
   and ``"kind": "m1_lite"`` so the M2 projector can detect and
   discard it without confusion.
3. Prove in tests that append+replay round-trip losslessly.

The fold is intentionally conservative — unknown ``kind`` values are
simply counted, never hard-failed, so M2 can extend the registry
without breaking the M1 replayer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from feishu_agent.team.artifact_store import ArtifactStore, RoleArtifact
from feishu_agent.team.task_event_log import TaskEvent

_SNAPSHOT_KIND = "m1_lite"


@dataclass
class MessageSummary:
    """One inbound/outbound record captured by ``message.*`` events."""

    direction: str  # "inbound" | "outbound"
    seq: int
    ts: str
    role: str | None
    content_preview: str


@dataclass
class ToolCallSummary:
    """One ``tool.call`` paired with its ``tool.result`` / ``tool.error``."""

    seq: int
    ts: str
    tool_name: str
    status: str  # "ok" | "error" | "pending"
    error: str | None = None


@dataclass
class TaskSnapshot:
    """Minimal replay snapshot used in M1.

    This is NOT the full :class:`TaskState` that M2 will build; it
    exists only so the CLI and equivalence tests have a stable target.
    """

    schema: str = _SNAPSHOT_KIND
    task_id: str | None = None
    last_seq: int = 0
    event_counts: dict[str, int] = field(default_factory=dict)
    messages: list[MessageSummary] = field(default_factory=list)
    tool_calls: list[ToolCallSummary] = field(default_factory=list)
    compressions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "task_id": self.task_id,
            "last_seq": self.last_seq,
            "event_counts": dict(self.event_counts),
            "messages": [vars(m) for m in self.messages],
            "tool_calls": [vars(t) for t in self.tool_calls],
            "compressions": self.compressions,
        }


def _preview(text: Any, *, limit: int = 200) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def replay(events: Iterable[TaskEvent]) -> TaskSnapshot:
    """Fold ``events`` into a :class:`TaskSnapshot`.

    Deterministic: same input → same output. Order-preserving.
    Unknown event kinds are counted but otherwise ignored.
    """
    snap = TaskSnapshot()
    pending_tool_calls: dict[str, ToolCallSummary] = {}

    for event in events:
        snap.task_id = snap.task_id or event.task_id
        snap.last_seq = max(snap.last_seq, event.seq)
        snap.event_counts[event.kind] = snap.event_counts.get(event.kind, 0) + 1
        payload = event.payload or {}

        if event.kind == "message.inbound":
            snap.messages.append(
                MessageSummary(
                    direction="inbound",
                    seq=event.seq,
                    ts=event.ts,
                    role=payload.get("role_name") or payload.get("role"),
                    content_preview=_preview(
                        payload.get("content") or payload.get("text") or ""
                    ),
                )
            )
        elif event.kind == "message.outbound":
            snap.messages.append(
                MessageSummary(
                    direction="outbound",
                    seq=event.seq,
                    ts=event.ts,
                    role=payload.get("role_name") or payload.get("role"),
                    content_preview=_preview(
                        payload.get("content") or payload.get("text") or ""
                    ),
                )
            )
        elif event.kind == "llm.compression":
            snap.compressions += 1
        elif event.kind == "tool.call":
            call_id = str(payload.get("call_id") or event.seq)
            summary = ToolCallSummary(
                seq=event.seq,
                ts=event.ts,
                tool_name=str(payload.get("tool_name") or ""),
                status="pending",
            )
            pending_tool_calls[call_id] = summary
            snap.tool_calls.append(summary)
        elif event.kind == "tool.result":
            call_id = str(payload.get("call_id") or "")
            if call_id and call_id in pending_tool_calls:
                pending_tool_calls[call_id].status = "ok"
                pending_tool_calls.pop(call_id, None)
        elif event.kind == "tool.error":
            call_id = str(payload.get("call_id") or "")
            if call_id and call_id in pending_tool_calls:
                pending_tool_calls[call_id].status = "error"
                pending_tool_calls[call_id].error = _preview(payload.get("error"))
                pending_tool_calls.pop(call_id, None)

    return snap


@dataclass
class TeamReplay:
    """Joined view of one team's event transcript + its artifacts
    (A-3 / T048).

    ``TaskSnapshot`` captures the raw event stream; this envelope
    layers the role-artifact envelopes on top so a single call
    returns everything needed for a human to reason about what
    the team did. Formatting is intentionally narrow — the CLI
    (``scripts/task_inspect.py``, future) and tests share the
    same output path via :meth:`format_lines`.
    """

    snapshot: TaskSnapshot
    artifacts: list[RoleArtifact] = field(default_factory=list)

    @classmethod
    def from_trace(
        cls,
        root_trace_id: str,
        *,
        events: Iterable[TaskEvent] | None = None,
        store: ArtifactStore | None = None,
        base_dir: Path | None = None,
    ) -> "TeamReplay":
        """Build a replay view for one team.

        Parameters are intentionally orthogonal so callers can mix
        concrete artifacts (``store``) with an external event
        iterator (``events``) in tests. In production, the CLI
        builds ``events`` from the task's ``events.jsonl`` and
        relies on ``base_dir`` to anchor the default
        :class:`ArtifactStore`.
        """
        if store is None:
            if base_dir is None:
                raise ValueError(
                    "TeamReplay.from_trace requires either a store or a base_dir"
                )
            store = ArtifactStore(base_dir)
        snapshot = replay(events or [])
        artifacts = store.list(root_trace_id)
        return cls(snapshot=snapshot, artifacts=artifacts)

    def format_lines(self) -> list[str]:
        """Human-readable one-line-per-concept projection.

        Mirrors the format from ``A-3-artifact-envelope.md``
        (``"[role] stop=... risk=..."`` plus one line per tool).
        Kept as a list of strings so tests can assert individual
        lines without juggling stdout capture.
        """
        lines: list[str] = []
        for art in self.artifacts:
            lines.append(
                f"[{art.role_name}] stop={art.stop_reason} "
                f"risk={art.risk_score:.2f} tools={len(art.tool_calls)} "
                f"success={art.success}"
            )
            for tc in art.tool_calls:
                marker = "×" if tc.is_error else "→"
                lines.append(
                    f"  {marker} {tc.tool_name} ({tc.duration_ms}ms)"
                )
        return lines


__all__ = [
    "TaskSnapshot",
    "MessageSummary",
    "ToolCallSummary",
    "TeamReplay",
    "replay",
]
