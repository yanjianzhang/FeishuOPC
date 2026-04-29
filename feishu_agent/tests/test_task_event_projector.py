"""Tests for :class:`TaskEventProjector`.

The projector's one job is to mirror a *curated* subset of ``HookBus``
events onto the per-task append-only log so the dual-write transition
in M1 doesn't leave session-level events (``on_session_start`` /
``on_session_end`` / ``on_sub_agent_spawn`` / …) missing from the
authoritative stream.

What we assert here:

1. Mirrored events land on the handle with the mapped ``kind``.
2. Non-mirrored events (``pre_llm_call`` etc., which the adapter
   already writes itself) are **not** double-written.
3. Exceptions from the handle are swallowed — the projector must
   never break the tool loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from feishu_agent.core.hook_bus import HookBus
from feishu_agent.team.task_event_log import TaskKey
from feishu_agent.team.task_event_projector import TaskEventProjector
from feishu_agent.team.task_service import TaskService


@pytest.mark.asyncio
async def test_projector_mirrors_session_events(tmp_path: Path) -> None:
    svc = TaskService(tasks_root=tmp_path)
    handle = svc.open_or_resume(
        TaskKey(bot_name="bot", chat_id="c1", root_id="r1"),
        role_name="tech_lead",
    )
    bus = HookBus()
    TaskEventProjector(handle).attach(bus)

    await bus.afire("on_session_start", {"trace_id": "t1", "role": "developer"})
    await bus.afire("on_session_end", {"trace_id": "t1", "role": "developer", "ok": True})
    await bus.afire("on_sub_agent_spawn", {"parent_trace_id": "t1", "child_trace_id": "t2"})

    events = handle.log.read_events()
    kinds = [e.kind for e in events]
    assert "task.meta.session_start" in kinds
    assert "task.meta.session_end" in kinds
    assert "task.meta.sub_agent_spawn" in kinds


@pytest.mark.asyncio
async def test_projector_ignores_unmapped_events(tmp_path: Path) -> None:
    """pre_llm_call / on_tool_call are emitted by the adapter itself.

    The projector must not double-write them onto the task log.
    """
    svc = TaskService(tasks_root=tmp_path)
    handle = svc.open_or_resume(
        TaskKey(bot_name="bot", chat_id="c1", root_id="r1"),
        role_name="tech_lead",
    )
    bus = HookBus()
    TaskEventProjector(handle).attach(bus)

    await bus.afire("pre_llm_call", {"trace_id": "t1", "turn": 0})
    await bus.afire("post_llm_call", {"trace_id": "t1", "turn": 0})
    await bus.afire("on_tool_call", {"trace_id": "t1", "tool_name": "x"})

    events = handle.log.read_events()
    adapter_kinds = [e.kind for e in events if e.kind.startswith(("llm.", "tool."))]
    assert adapter_kinds == []


@pytest.mark.asyncio
async def test_projector_swallows_handle_errors(tmp_path: Path) -> None:
    """A broken handle must not surface through the bus."""
    svc = TaskService(tasks_root=tmp_path)
    handle = svc.open_or_resume(
        TaskKey(bot_name="bot", chat_id="c1", root_id="r1"),
        role_name="tech_lead",
    )

    def _boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("disk full")

    handle.append = _boom  # type: ignore[assignment]

    bus = HookBus()
    TaskEventProjector(handle).attach(bus)

    await bus.afire("on_session_start", {"trace_id": "t1"})
