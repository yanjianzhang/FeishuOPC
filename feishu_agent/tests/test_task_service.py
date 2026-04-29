from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from feishu_agent.team.task_event_log import (
    TaskEvent,
    TaskEventLog,
    TaskKey,
)
from feishu_agent.team.task_service import TaskService


def test_task_key_derivation_is_stable(tmp_path: Path) -> None:
    a = TaskKey.derive(
        bot_name="tech_lead",
        chat_id="oc_chat_1",
        root_id="om_root_1",
        message_id="om_msg_A",
    )
    b = TaskKey.derive(
        bot_name="tech_lead",
        chat_id="oc_chat_1",
        root_id="om_root_1",
        message_id="om_msg_B",  # different message, same thread
    )
    assert a == b
    assert a.task_id() == b.task_id()
    # Different root_id => different task
    c = TaskKey.derive(
        bot_name="tech_lead",
        chat_id="oc_chat_1",
        root_id="om_root_2",
        message_id="om_msg_C",
    )
    assert c != a
    assert c.task_id() != a.task_id()


def test_task_key_falls_back_to_message_id(tmp_path: Path) -> None:
    key = TaskKey.derive(
        bot_name="tech_lead",
        chat_id="oc_chat_1",
        root_id=None,
        message_id="om_msg_single",
    )
    assert key.root_id == "om_msg_single"


def test_event_log_appends_and_scans(tmp_path: Path) -> None:
    log = TaskEventLog(tmp_path / "tl-abcdef012345")
    ev1 = log.append(kind="message.inbound", payload={"text": "hello"})
    ev2 = log.append(kind="llm.request", payload={"turn": 0})
    assert ev1.seq == 0
    assert ev2.seq == 1

    events = log.read_events()
    assert [e.kind for e in events] == ["message.inbound", "llm.request"]

    # Reopen the log and make sure we pick up seq counter from disk.
    reopened = TaskEventLog(tmp_path / "tl-abcdef012345")
    ev3 = reopened.append(kind="llm.response", payload={"turn": 0})
    assert ev3.seq == 2


def test_event_log_snapshot_round_trips(tmp_path: Path) -> None:
    log = TaskEventLog(tmp_path / "tl-snapshot")
    log.append(kind="message.inbound", payload={"text": "hi"})
    log.write_snapshot({"base_seq": 0, "mode": "agent", "todos": []})
    snap = log.read_snapshot()
    assert snap == {"base_seq": 0, "mode": "agent", "todos": []}


def test_event_log_rejects_unsafe_dirname(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        TaskEventLog(tmp_path / "bad name with spaces")


def test_event_from_json_round_trip() -> None:
    ev = TaskEvent(
        task_id="tl-abc",
        seq=3,
        kind="tool.call",
        trace_id="trace-1",
        payload={"tool": "read_workflow_instruction"},
    )
    raw = ev.to_json()
    parsed = json.loads(raw)
    assert parsed["kind"] == "tool.call"
    back = TaskEvent.from_json(raw)
    assert back is not None
    assert back.seq == 3
    assert back.payload == {"tool": "read_workflow_instruction"}


def test_task_service_open_then_resume(tmp_path: Path) -> None:
    svc = TaskService(tmp_path / "tasks")
    key = TaskKey.derive(
        bot_name="tech_lead",
        chat_id="oc_chat_1",
        root_id="om_root_1",
        message_id="om_msg_A",
    )
    handle = svc.open_or_resume(key, role_name="tech_lead", project_id="proj")
    assert handle.meta.role_name == "tech_lead"
    assert handle.meta.project_id == "proj"
    # Opens emit a ``task.opened`` event.
    kinds = [e.kind for e in handle.events()]
    assert kinds == ["task.opened"]

    # Open a NEW service instance (simulates process restart) with the
    # same tasks_root — should resume (no second task.opened, one
    # task.resumed event).
    svc2 = TaskService(tmp_path / "tasks")
    handle2 = svc2.open_or_resume(key, role_name="tech_lead", project_id="proj")
    kinds2 = [e.kind for e in handle2.events()]
    assert kinds2 == ["task.opened", "task.resumed"]


def test_task_service_cache_returns_same_handle(tmp_path: Path) -> None:
    svc = TaskService(tmp_path / "tasks")
    key = TaskKey.derive(
        bot_name="tech_lead",
        chat_id="oc_chat_1",
        root_id="om_root_2",
        message_id="om_msg_A",
    )
    h1 = svc.open_or_resume(key)
    h2 = svc.open_or_resume(key)
    assert h1 is h2


def test_task_service_session_lock_serializes() -> None:
    async def main() -> None:
        svc = TaskService(Path("/tmp/feishu-tasks-test-lock"))
        key = TaskKey.derive(
            bot_name="tech_lead",
            chat_id="oc_chat_lock",
            root_id="om_root_lock",
            message_id="om_msg",
        )
        handle = svc.open_or_resume(key)
        order: list[str] = []

        async def worker(name: str) -> None:
            async with handle.session_lock():
                order.append(f"{name}:start")
                await asyncio.sleep(0.01)
                order.append(f"{name}:end")

        await asyncio.gather(worker("a"), worker("b"))
        # b cannot start before a finishes (or vice versa).
        pairs = ["a:start", "a:end", "b:start", "b:end"]
        reverse = ["b:start", "b:end", "a:start", "a:end"]
        assert order == pairs or order == reverse

    asyncio.run(main())
