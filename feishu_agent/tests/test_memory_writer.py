from __future__ import annotations

import pytest

from feishu_agent.core.hook_bus import HookBus
from feishu_agent.team.agent_notes_service import AgentNotesService
from feishu_agent.team.memory_writer import MemoryWriterService
from feishu_agent.team.task_event_log import TaskKey
from feishu_agent.team.task_service import TaskService


def test_memory_writer_generates_candidates_from_tagged_notes(tmp_path) -> None:
    task_service = TaskService(tasks_root=tmp_path / "tasks")
    handle = task_service.open_or_resume(
        TaskKey(bot_name="bot", chat_id="c1", root_id="r1"),
        role_name="tech_lead",
        project_id="demo",
    )
    handle.append(
        kind="state.note_added",
        payload={"text": "feature 分支统一用 feature/<story>-<slug>", "tags": ["decision"]},
    )
    handle.append(
        kind="state.note_added",
        payload={"text": "只是普通备注", "tags": []},
    )
    notes = AgentNotesService(project_id="demo", project_root=tmp_path / "repo")
    notes.append(
        role="tech_lead",
        note="已有的持久记忆，不要重复建议",
    )
    handle.append(
        kind="state.note_added",
        payload={"text": "已有的持久记忆，不要重复建议", "tags": ["memory"]},
    )

    writer = MemoryWriterService(task_handle=handle, notes_service=notes)
    candidates = writer.generate_candidates()

    assert candidates.add_note_candidates == ["feature 分支统一用 feature/<story>-<slug>"]
    assert isinstance(candidates.session_summary_update, dict)
    assert "summary_text" in candidates.session_summary_update


def test_stale_candidates_detect_chinese_blocker_marker(tmp_path) -> None:
    """Regression for H1: the stale-note heuristic must work on Chinese
    notes. Previously it only checked the English substring ``blocked``
    and therefore missed ``阻塞``/``卡住`` etc. on a Chinese-first repo."""

    task_service = TaskService(tasks_root=tmp_path / "tasks")
    handle = task_service.open_or_resume(
        TaskKey(bot_name="bot", chat_id="c1", root_id="r1"),
        role_name="tech_lead",
        project_id="demo",
    )
    # A recent thread that has no active blockers anymore — so a historical
    # note about a blocker should now read as stale.
    handle.append(
        kind="message.inbound",
        trace_id="t1",
        payload={"command_text": "继续推进"},
    )

    notes = AgentNotesService(project_id="demo", project_root=tmp_path / "repo")
    notes.append(role="tech_lead", note="发布流水线阻塞在 reviewer 环节")
    notes.append(role="tech_lead", note="flutter release 用 --release 开关")

    writer = MemoryWriterService(task_handle=handle, notes_service=notes)
    candidates = writer.generate_candidates()

    assert any("阻塞" in text for text in candidates.stale_note_candidates), (
        "chinese blocker marker 阻塞 must be detected as stale when the "
        "current session carries no blockers"
    )


def test_stale_candidates_skip_notes_when_blocker_still_live(tmp_path) -> None:
    """Inverse of the regression: when the current session is still
    actively blocked (or carries a matching marker), historical notes
    about the same kind of blocker must NOT be flagged as stale."""

    task_service = TaskService(tasks_root=tmp_path / "tasks")
    handle = task_service.open_or_resume(
        TaskKey(bot_name="bot", chat_id="c1", root_id="r1"),
        role_name="tech_lead",
        project_id="demo",
    )
    handle.append(
        kind="message.inbound",
        trace_id="t1",
        payload={"command_text": "发布流水线依然阻塞"},
    )
    handle.append(
        kind="state.tool_health_updated",
        payload={"tool_name": "deploy", "online": False, "last_error": "阻塞"},
    )

    notes = AgentNotesService(project_id="demo", project_root=tmp_path / "repo")
    notes.append(role="tech_lead", note="发布流水线阻塞在 reviewer 环节")

    writer = MemoryWriterService(task_handle=handle, notes_service=notes)
    candidates = writer.generate_candidates()
    assert candidates.stale_note_candidates == []


@pytest.mark.asyncio
async def test_memory_writer_attaches_and_appends_event(tmp_path) -> None:
    task_service = TaskService(tasks_root=tmp_path / "tasks")
    handle = task_service.open_or_resume(
        TaskKey(bot_name="bot", chat_id="c1", root_id="r1"),
        role_name="tech_lead",
        project_id="demo",
    )
    handle.append(
        kind="state.note_added",
        payload={"text": "记录一个决策", "tags": ["decision"]},
    )
    bus = HookBus()
    MemoryWriterService(
        task_handle=handle,
        notes_service=AgentNotesService(project_id="demo", project_root=tmp_path / "repo"),
    ).attach(bus)

    await bus.afire("on_session_end", {"trace_id": "trace-1", "ok": True})

    events = handle.log.read_events()
    memory_events = [e for e in events if e.kind == "memory.candidates_generated"]
    assert len(memory_events) == 1
    payload = memory_events[0].payload
    assert payload["add_note_candidates"] == ["记录一个决策"]
    assert "session_summary_update" in payload
