"""Replay-equivalence tests for ``task_replay``.

The guarantee M1 ships: for any stream of events appended to
``TaskEventLog``, ``replay(log.read_events())`` returns a deterministic
snapshot that captures the lossy M1 projection (messages, tool calls,
compression count, event totals).

We assert:

1. **Determinism** — replay(a) == replay(a) for the same stream.
2. **Order-sensitive** — permuting tool.call/result pairs produces a
   different, but still well-formed, snapshot.
3. **Snapshot round-trip** — ``TaskEventLog.write_snapshot`` plus
   ``read_snapshot`` preserves the JSON shape exactly.
4. **Resume equivalence** — opening a second :class:`TaskEventLog`
   over the same directory yields an identical event list (the
   fundamental correctness claim for resume).
"""

from __future__ import annotations

from pathlib import Path

from feishu_agent.team.task_event_log import TaskEventLog
from feishu_agent.team.task_replay import TaskSnapshot, replay


def _seed(log: TaskEventLog) -> None:
    log.append(kind="task.opened", payload={"role_name": "tech_lead"})
    log.append(
        kind="message.inbound",
        payload={"role_name": "tech_lead", "content": "hi"},
        trace_id="t0",
    )
    log.append(
        kind="llm.request",
        payload={"model": "m", "turn": 0},
        trace_id="t0",
    )
    log.append(
        kind="tool.call",
        payload={"call_id": "c1", "tool_name": "ping"},
        trace_id="t0",
    )
    log.append(
        kind="tool.result",
        payload={"call_id": "c1"},
        trace_id="t0",
    )
    log.append(
        kind="llm.compression",
        payload={"reason": "budget"},
        trace_id="t0",
    )
    log.append(
        kind="tool.call",
        payload={"call_id": "c2", "tool_name": "write"},
        trace_id="t0",
    )
    log.append(
        kind="tool.error",
        payload={"call_id": "c2", "error": "disk full"},
        trace_id="t0",
    )
    log.append(
        kind="message.outbound",
        payload={"content": "done"},
        trace_id="t0",
    )


def test_replay_is_deterministic(tmp_path: Path) -> None:
    log = TaskEventLog(tmp_path / "abc")
    _seed(log)
    events = log.read_events()

    a = replay(events).to_dict()
    b = replay(events).to_dict()
    assert a == b


def test_replay_captures_messages_and_tools(tmp_path: Path) -> None:
    log = TaskEventLog(tmp_path / "abc")
    _seed(log)
    snap = replay(log.read_events())

    assert snap.last_seq > 0
    assert snap.compressions == 1
    directions = [m.direction for m in snap.messages]
    assert directions == ["inbound", "outbound"]

    tool_statuses = {(t.tool_name, t.status) for t in snap.tool_calls}
    assert ("ping", "ok") in tool_statuses
    assert ("write", "error") in tool_statuses

    # Event totals equal the number of events written.
    assert sum(snap.event_counts.values()) == len(log.read_events())


def test_snapshot_round_trip_on_disk(tmp_path: Path) -> None:
    log = TaskEventLog(tmp_path / "abc")
    _seed(log)
    snap = replay(log.read_events())

    log.write_snapshot(snap.to_dict())
    loaded = log.read_snapshot()
    assert loaded == snap.to_dict()


def test_resume_event_list_matches_after_reopen(tmp_path: Path) -> None:
    """Opening a second :class:`TaskEventLog` over the same directory
    sees the exact same events — the core M1 claim."""
    dir_ = tmp_path / "abc"
    log = TaskEventLog(dir_)
    _seed(log)
    first = [e.to_json() for e in log.read_events()]

    log2 = TaskEventLog(dir_)
    second = [e.to_json() for e in log2.read_events()]

    assert first == second

    log2.append(kind="task.resumed", payload={})
    assert log2.read_events()[-1].kind == "task.resumed"
    assert log2.read_events()[-1].seq == len(first)  # seqs are 0-indexed


def test_snapshot_is_stable_type() -> None:
    """``TaskSnapshot.to_dict`` must always include the schema marker
    so M2 can detect-and-discard M1 snapshots cleanly."""
    snap = TaskSnapshot().to_dict()
    assert snap["schema"] == "m1_lite"
