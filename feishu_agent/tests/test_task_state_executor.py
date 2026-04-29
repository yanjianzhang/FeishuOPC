"""Tests for :class:`TaskStateExecutor`.

Contract under test:

1. Each tool call emits exactly one event with the mapped ``kind``.
2. Invalid arguments return ``{"ok": False, "error": "INVALID_ARGUMENTS"}``
   without touching the event log.
3. The emitted events, when replayed through
   :class:`TaskStateProjector`, reconstruct the exact state the LLM
   intended to set.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from feishu_agent.team.task_event_log import TaskKey
from feishu_agent.team.task_service import TaskService
from feishu_agent.team.task_state import TaskStateProjector
from feishu_agent.team.task_state_executor import TaskStateExecutor


@pytest.fixture()
def handle(tmp_path: Path):
    svc = TaskService(tasks_root=tmp_path)
    return svc.open_or_resume(
        TaskKey(bot_name="bot", chat_id="c1", root_id="r1"),
        role_name="tech_lead",
    )


@pytest.mark.asyncio
async def test_set_mode_emits_mode_set(handle) -> None:
    exec_ = TaskStateExecutor(handle)
    res = await exec_.execute_tool("set_mode", {"mode": "plan", "reason": "need to think"})

    assert res["ok"] is True
    assert res["event"]["kind"] == "state.mode_set"

    events = handle.log.read_events()
    kinds = [e.kind for e in events]
    assert "state.mode_set" in kinds


@pytest.mark.asyncio
async def test_set_plan_assigns_step_indices(handle) -> None:
    exec_ = TaskStateExecutor(handle)
    res = await exec_.execute_tool(
        "set_plan",
        {
            "title": "Feature X",
            "steps": [{"title": "design"}, {"title": "implement"}],
        },
    )

    assert res["ok"] is True
    event = next(e for e in handle.log.read_events() if e.kind == "state.plan_set")
    steps = event.payload["steps"]
    assert [s["index"] for s in steps] == [0, 1]


@pytest.mark.asyncio
async def test_add_todo_returns_generated_id(handle) -> None:
    exec_ = TaskStateExecutor(handle)
    res = await exec_.execute_tool("add_todo", {"text": "clean /tmp"})

    assert res["ok"] is True
    tid = res["id"]
    assert tid.startswith("todo-")

    event = next(e for e in handle.log.read_events() if e.kind == "state.todo_added")
    assert event.payload["id"] == tid


@pytest.mark.asyncio
async def test_invalid_arguments_return_structured_error(handle) -> None:
    exec_ = TaskStateExecutor(handle)
    res = await exec_.execute_tool("set_mode", {})  # missing required 'mode'

    assert res["ok"] is False
    assert res["error"] == "INVALID_ARGUMENTS"
    # No state.* event should have been appended; task.opened from
    # open_or_resume is the only event present.
    kinds = [e.kind for e in handle.log.read_events()]
    assert not any(k.startswith("state.") for k in kinds)


@pytest.mark.asyncio
async def test_unknown_tool_is_reported(handle) -> None:
    exec_ = TaskStateExecutor(handle)
    res = await exec_.execute_tool("does_not_exist", {})

    assert res["ok"] is False
    assert "UNKNOWN_TOOL" in res["error"]


@pytest.mark.asyncio
async def test_full_flow_round_trips_through_projector(handle) -> None:
    """End-to-end: executor tools → events → projected TaskState.

    The projector must see exactly what the LLM "said".
    """
    exec_ = TaskStateExecutor(handle)

    await exec_.execute_tool("set_mode", {"mode": "plan"})
    await exec_.execute_tool(
        "set_plan",
        {
            "title": "Ship X",
            "summary": "outline",
            "steps": [{"title": "design"}, {"title": "implement"}],
        },
    )
    add_res = await exec_.execute_tool("add_todo", {"text": "write tests"})
    await exec_.execute_tool(
        "update_todo", {"id": add_res["id"], "status": "in_progress"}
    )
    await exec_.execute_tool("mark_todo_done", {"id": add_res["id"]})
    await exec_.execute_tool("set_mode", {"mode": "act"})
    await exec_.execute_tool("note", {"text": "nothing surprising"})

    state = TaskStateProjector().project(handle.log.read_events())
    assert state.mode == "act"
    assert state.plan.title == "Ship X"
    assert len(state.plan.steps) == 2
    assert state.todos[add_res["id"]].status == "done"
    assert len(state.notes) == 1


def test_tool_specs_expose_all_tools(handle) -> None:
    exec_ = TaskStateExecutor(handle)
    names = {spec.name for spec in exec_.tool_specs()}
    assert names == {
        "set_mode",
        "set_plan",
        "update_plan_step",
        "add_todo",
        "update_todo",
        "mark_todo_done",
        "note",
    }
    # Every spec must have a JSON schema with at least a ``type``.
    for spec in exec_.tool_specs():
        assert spec.input_schema.get("type") == "object"
