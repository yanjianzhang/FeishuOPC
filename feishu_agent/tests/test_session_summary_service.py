from __future__ import annotations

from feishu_agent.team.session_summary_service import SessionSummaryService
from feishu_agent.team.task_event_log import TaskKey
from feishu_agent.team.task_service import TaskService


def test_session_summary_builds_from_task_events(tmp_path) -> None:
    svc = TaskService(tasks_root=tmp_path)
    handle = svc.open_or_resume(
        TaskKey(bot_name="bot", chat_id="c1", root_id="r1"),
        role_name="tech_lead",
        project_id="demo",
    )
    handle.append(
        kind="message.inbound",
        trace_id="t0",
        payload={"command_text": "继续推进 story 3-1 到 review"},
    )
    handle.append(kind="state.mode_set", payload={"mode": "plan"})
    handle.append(
        kind="state.plan_set",
        payload={
            "title": "推进 3-1",
            "steps": [{"index": 0, "title": "inspect CI"}],
        },
    )
    handle.append(
        kind="state.plan_step_updated",
        payload={"index": 0, "status": "in_progress"},
    )
    handle.append(
        kind="state.todo_added",
        payload={"id": "todo-1", "text": "修 lint", "status": "open"},
    )
    handle.append(
        kind="state.note_added",
        payload={"text": "feature 分支命名保持 feature/<story>-<slug>", "tags": ["decision"]},
    )
    handle.append(
        kind="pending.requested",
        payload={"pending_id": "p1", "action_type": "git_push"},
    )
    handle.append(
        kind="message.outbound",
        trace_id="t0",
        payload={"content_preview": "我会先检查 CI，再决定是否推进到 review。"},
    )
    handle.append(kind="llm.compression", payload={"turn": 3})

    summary = SessionSummaryService().build_for_handle(handle)

    assert summary.current_mode == "plan"
    assert summary.plan_title == "推进 3-1"
    assert summary.compressions == 1
    assert "Latest thread focus" in summary.summary_text
    assert any("Plan step 1" in item for item in summary.open_loops)
    assert any("Todo todo-1" in item for item in summary.open_loops)
    assert any("Pending confirmation p1" in item for item in summary.pending_blockers)
    assert summary.recent_decisions == ["feature 分支命名保持 feature/<story>-<slug>"]
    rendered = summary.render_for_prompt()
    assert "## Session summary" in rendered
    assert "Current mode" in rendered


def test_session_summary_can_exclude_current_trace_messages(tmp_path) -> None:
    svc = TaskService(tasks_root=tmp_path)
    handle = svc.open_or_resume(
        TaskKey(bot_name="bot", chat_id="c1", root_id="r1"),
        role_name="tech_lead",
    )
    handle.append(
        kind="message.inbound",
        trace_id="old-trace",
        payload={"command_text": "先看一下旧问题"},
    )
    handle.append(
        kind="message.outbound",
        trace_id="old-trace",
        payload={"content_preview": "上一轮停在 reviewer。"},
    )
    handle.append(
        kind="message.inbound",
        trace_id="new-trace",
        payload={"command_text": "这是本轮的新问题"},
    )

    summary = SessionSummaryService().build_for_handle(
        handle, exclude_trace_id="new-trace"
    )

    assert summary.last_user_message == "先看一下旧问题"
    assert summary.last_assistant_message == "上一轮停在 reviewer。"
