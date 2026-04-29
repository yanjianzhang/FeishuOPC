from __future__ import annotations

from feishu_agent.team.agent_notes_service import AgentNotesService
from feishu_agent.team.last_run_memory_service import (
    LastRunMemoryService,
    RunDigest,
)
from feishu_agent.team.memory_assembler import (
    MemoryAssembler,
    MemoryQueryContext,
    build_transient_reminder_fragment,
)
from feishu_agent.team.task_event_log import TaskKey
from feishu_agent.team.task_service import TaskService


def test_memory_assembler_builds_unified_prompt_suffix(tmp_path) -> None:
    task_service = TaskService(tasks_root=tmp_path / "tasks")
    handle = task_service.open_or_resume(
        TaskKey(bot_name="bot", chat_id="c1", root_id="r1"),
        role_name="tech_lead",
        project_id="demo",
    )
    handle.append(kind="state.mode_set", payload={"mode": "plan"})
    handle.append(
        kind="message.inbound",
        trace_id="old",
        payload={"command_text": "上轮卡在 reviewer lint"},
    )

    project_root = tmp_path / "repo"
    notes = AgentNotesService(project_id="demo", project_root=project_root)
    notes.append(
        role="tech_lead",
        note="Flutter release build on Windows must use --release to avoid OOM.",
    )
    notes.append(
        role="tech_lead",
        note="Postgres migration order must match production replicas.",
    )

    last_run = LastRunMemoryService(project_id="demo", project_root=project_root)
    last_run.append(
        RunDigest(
            trace_id="t-fail",
            started_at="2026-04-22T10:00:00+00:00",
            stop_reason="error",
            ok=False,
            error_detail="reviewer: lint failed",
        )
    )

    assembly = MemoryAssembler().build(
        MemoryQueryContext(
            role_name="tech_lead",
            project_id="demo",
            user_query="处理 flutter release OOM 问题",
            task_handle=handle,
            notes_service=notes,
            last_run_service=last_run,
            baseline_fragment="## Runtime baseline\n\n- branch: `feature/x`\n",
            notes_limit=1,
        )
    )

    suffix = assembly.system_prompt_suffix()
    assert "## Project memory" in suffix
    assert "Flutter release build" in suffix
    assert "Postgres migration" not in suffix
    assert "## Last run context" in suffix
    assert "## Session summary" in suffix
    assert "## Runtime baseline" in suffix


def test_memory_assembler_builds_transient_reminder_fragment(tmp_path) -> None:
    task_service = TaskService(tasks_root=tmp_path / "tasks")
    handle = task_service.open_or_resume(
        TaskKey(bot_name="bot", chat_id="c1", root_id="r1"),
        role_name="tech_lead",
    )
    handle.append(kind="state.mode_set", payload={"mode": "plan"})

    fragment = MemoryAssembler().build_transient_reminder_fragment(handle)

    assert fragment is not None
    assert fragment.transient is True
    assert "<system_reminder>" in fragment.content
    assert "plan_mode" in (fragment.metadata.get("rule_ids") or [])


def test_module_level_reminder_helper_matches_assembler(tmp_path) -> None:
    """The adapter relies on the module-level helper to avoid reconstructing
    a :class:`MemoryAssembler` (and its ``SessionSummaryService``) on every
    turn. Keep its output byte-identical to the assembler delegate."""

    task_service = TaskService(tasks_root=tmp_path / "tasks")
    handle = task_service.open_or_resume(
        TaskKey(bot_name="bot", chat_id="c1", root_id="r1"),
        role_name="tech_lead",
    )
    handle.append(kind="state.mode_set", payload={"mode": "plan"})

    via_helper = build_transient_reminder_fragment(handle)
    via_assembler = MemoryAssembler().build_transient_reminder_fragment(handle)

    assert via_helper is not None and via_assembler is not None
    assert via_helper.content == via_assembler.content
    assert via_helper.metadata == via_assembler.metadata


def test_memory_assembler_skips_task_mode_bonus_when_summary_empty(tmp_path) -> None:
    """Regression for H2: the default ``current_mode='act'`` on an empty
    :class:`SessionSummary` must not boost notes that happen to include
    the English token "act"."""

    project_root = tmp_path / "repo"
    notes = AgentNotesService(project_id="demo", project_root=project_root)
    notes.append(role="tech_lead", note="act 分支先 merge 再切 release")
    notes.append(role="tech_lead", note="上线前记得跑 flutter release")

    assembly = MemoryAssembler().build(
        MemoryQueryContext(
            role_name="tech_lead",
            project_id="demo",
            user_query="flutter release",
            task_handle=None,  # no thread state → summary is empty
            notes_service=notes,
            notes_limit=2,
        )
    )

    durable = assembly.durable_fragments[0]
    # task_mode key is only present when summary is non-empty
    assert "task_mode" not in durable.metadata
    # the query-relevant note must rank first in the rendered block
    suffix = assembly.system_prompt_suffix()
    act_pos = suffix.find("act 分支")
    release_pos = suffix.find("flutter release")
    assert release_pos != -1 and act_pos != -1
    assert release_pos < act_pos, (
        "without a real task_mode signal, query-relevant notes should not "
        "be outranked by notes containing the English token 'act'"
    )


def test_ordered_durable_fragments_excludes_transient_reminder(tmp_path) -> None:
    """Regression for L1: the rename makes it explicit that transient
    reminders do NOT live in the system-prompt ordering."""

    task_service = TaskService(tasks_root=tmp_path / "tasks")
    handle = task_service.open_or_resume(
        TaskKey(bot_name="bot", chat_id="c1", root_id="r1"),
        role_name="tech_lead",
    )
    handle.append(kind="state.mode_set", payload={"mode": "plan"})
    assembly = MemoryAssembler().build(
        MemoryQueryContext(
            role_name="tech_lead",
            user_query="noop",
            task_handle=handle,
            baseline_fragment="",
        )
    )
    ordered = assembly.ordered_durable_fragments()
    assert assembly.transient_reminder_fragment is not None
    assert assembly.transient_reminder_fragment not in ordered
