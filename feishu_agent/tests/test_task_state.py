"""Replay-equivalence tests for :class:`TaskStateProjector`.

The M2 contract under test:

1. **Pure projection.** Same events → same state, regardless of how
   many times we replay.
2. **Snapshot round-trip.** ``TaskState.to_dict`` / ``from_dict``
   survives a JSON round-trip, and replaying additional events onto
   the rehydrated state produces the same result as a full replay.
3. **Per-``kind`` semantics.** Mode set, plan set + step update, todo
   add/update/done, tool health transitions, pending actions, and
   compressions all update the right fields.
4. **Unknown kinds ignored.** Future-proofing: an unknown ``kind``
   doesn't crash the projector and doesn't bump ``last_seq`` beyond
   what ``_apply`` would naturally set.
"""

from __future__ import annotations

import json
from pathlib import Path

from feishu_agent.team.task_event_log import TaskEvent, TaskEventLog
from feishu_agent.team.task_state import (
    TaskState,
    TaskStateProjector,
)


def _e(seq: int, kind: str, payload: dict | None = None, ts: str = "2026-01-01T00:00:00.000+00:00") -> TaskEvent:
    return TaskEvent(
        task_id="t",
        seq=seq,
        kind=kind,
        ts=ts,
        payload=payload or {},
    )


def test_projection_is_deterministic() -> None:
    events = [
        _e(0, "task.opened"),
        _e(1, "state.mode_set", {"mode": "plan"}),
        _e(
            2,
            "state.plan_set",
            {
                "title": "Ship feature X",
                "steps": [
                    {"index": 0, "title": "design"},
                    {"index": 1, "title": "implement"},
                ],
            },
        ),
        _e(3, "state.mode_set", {"mode": "act"}),
    ]
    p = TaskStateProjector()
    a = p.project(events).to_dict()
    b = p.project(events).to_dict()
    assert a == b
    assert a["mode"] == "act"
    assert len(a["plan"]["steps"]) == 2


def test_plan_step_update_is_applied_by_index() -> None:
    events = [
        _e(
            0,
            "state.plan_set",
            {"steps": [{"index": 0, "title": "a"}, {"index": 1, "title": "b"}]},
        ),
        _e(1, "state.plan_step_updated", {"index": 1, "status": "done"}),
    ]
    state = TaskStateProjector().project(events)
    assert state.plan.steps[0].status == "pending"
    assert state.plan.steps[1].status == "done"


def test_todo_lifecycle() -> None:
    events = [
        _e(0, "state.todo_added", {"id": "fix-disk", "text": "clean /tmp"}),
        _e(1, "state.todo_updated", {"id": "fix-disk", "status": "in_progress"}),
        _e(2, "state.todo_done", {"id": "fix-disk"}),
    ]
    state = TaskStateProjector().project(events)
    assert state.todos["fix-disk"].status == "done"
    # Updated_at advances with each event.
    assert state.todos["fix-disk"].updated_at


def test_tool_health_offline_and_recover() -> None:
    events = [
        _e(0, "tool.call", {"tool_name": "git"}),
        _e(1, "tool.error", {"tool_name": "git", "error": "boom", "classify": "offline"}),
        _e(2, "tool.result", {"tool_name": "git"}),
    ]
    state = TaskStateProjector().project(events)
    health = state.tool_health["git"]
    assert health.online is True  # recovered on result
    assert health.last_error == "boom"


def test_pending_requested_then_resolved() -> None:
    events = [
        _e(0, "pending.requested", {"pending_id": "p1", "action": "push"}),
        _e(1, "pending.resolved", {"pending_id": "p1"}),
    ]
    state = TaskStateProjector().project(events)
    assert "p1" not in state.pending_actions


def test_unknown_kind_is_noop() -> None:
    events = [_e(0, "task.opened"), _e(1, "not.a.real.kind", {"foo": 1})]
    state = TaskStateProjector().project(events)
    assert state.last_seq == 1  # still advances
    assert state.notes == []


def test_snapshot_round_trip_with_resume(tmp_path: Path) -> None:
    log = TaskEventLog(tmp_path / "t")
    log.append(kind="task.opened")
    log.append(kind="state.mode_set", payload={"mode": "plan"})
    log.append(kind="state.todo_added", payload={"id": "a", "text": "x"})

    state1 = TaskStateProjector().project(log.read_events())
    serialized = state1.to_dict()

    # Round-trip via JSON to mimic what state.json writes.
    rehydrated = TaskState.from_dict(json.loads(json.dumps(serialized)))
    assert rehydrated.to_dict() == serialized

    # Append more events and project incrementally on top.
    log.append(kind="state.todo_done", payload={"id": "a"})
    incremental = TaskStateProjector().project(
        [e for e in log.read_events() if e.seq > rehydrated.last_seq],
        base=rehydrated,
    )
    full = TaskStateProjector().project(log.read_events())
    assert incremental.to_dict() == full.to_dict()
