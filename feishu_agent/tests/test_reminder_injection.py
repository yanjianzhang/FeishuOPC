"""Adapter-level test: reminders injected into request, emitted as events.

Integration guarantees
----------------------
1. When the task is in ``plan`` mode, the LLM request payload contains
   a trailing user message whose content is a ``<system_reminder>``
   block mentioning the plan-mode rule. The persistent ``messages``
   history is NOT mutated (reminder is transient per turn).
2. A ``reminder.emitted`` event is appended to the task log on each
   turn that injects reminders, carrying the rule ids that fired.
3. With no task_handle, the path is a no-op — no crash, no injection.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from feishu_agent.core.llm_agent_adapter import AgentHandle, LlmAgentAdapter
from feishu_agent.team.task_event_log import TaskKey
from feishu_agent.team.task_service import TaskService


class _NullTool:
    def tool_specs(self):
        return []

    async def execute_tool(self, tool_name, arguments):
        return {}


def _make_adapter_with_stub(sent_payloads: list[dict[str, Any]]) -> LlmAgentAdapter:
    adapter = LlmAgentAdapter(
        llm_base_url="http://example.invalid",
        llm_api_key="k",
        default_model="m",
        timeout=30,
    )
    adapter._http = object()  # type: ignore[assignment]

    async def _stub_send(*, payload, timeout):
        sent_payloads.append(payload)
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }

    adapter._send_chat_completion = _stub_send  # type: ignore[assignment]
    return adapter


@pytest.mark.asyncio
async def test_plan_mode_reminder_injects_into_request(tmp_path: Path) -> None:
    svc = TaskService(tasks_root=tmp_path)
    handle = svc.open_or_resume(
        TaskKey(bot_name="bot", chat_id="c1", root_id="r1"),
        role_name="tech_lead",
    )
    # Seed plan mode so the plan_mode + empty_plan rules fire.
    handle.append(kind="state.mode_set", payload={"mode": "plan"})

    sent: list[dict[str, Any]] = []
    adapter = _make_adapter_with_stub(sent)

    agent = AgentHandle(agent_id="a", system_prompt="sys", model="m")
    result = await adapter.execute_with_tools(
        agent,
        "hello",
        _NullTool(),
        task_handle=handle,
        trace_id="trace-1",
    )

    assert result.success is True
    assert len(sent) == 1
    msgs = sent[0]["messages"]
    # The last sent message should be a transient user reminder.
    assert msgs[-1]["role"] == "user"
    assert "<system_reminder>" in msgs[-1]["content"]
    assert "plan mode" in msgs[-1]["content"].lower()

    # Task log must have reminder.emitted with both rule ids.
    events = handle.log.read_events()
    reminders = [e for e in events if e.kind == "reminder.emitted"]
    assert len(reminders) == 1
    rule_ids = set(reminders[0].payload["rule_ids"])
    assert "plan_mode" in rule_ids
    assert "empty_plan" in rule_ids


@pytest.mark.asyncio
async def test_no_handle_means_no_injection(tmp_path: Path) -> None:
    sent: list[dict[str, Any]] = []
    adapter = _make_adapter_with_stub(sent)

    agent = AgentHandle(agent_id="a", system_prompt="sys", model="m")
    result = await adapter.execute_with_tools(
        agent,
        "hello",
        _NullTool(),
        task_handle=None,
        trace_id="trace-1",
    )

    assert result.success is True
    assert len(sent) == 1
    msgs = sent[0]["messages"]
    # No reminder appended: the payload should only have the system +
    # original user message.
    assert all("<system_reminder>" not in (m.get("content") or "") for m in msgs)


@pytest.mark.asyncio
async def test_no_applicable_rules_means_no_injection(tmp_path: Path) -> None:
    """Even with a handle, an unremarkable state must not inject."""
    svc = TaskService(tasks_root=tmp_path)
    handle = svc.open_or_resume(
        TaskKey(bot_name="bot", chat_id="c1", root_id="r1"),
        role_name="tech_lead",
    )

    sent: list[dict[str, Any]] = []
    adapter = _make_adapter_with_stub(sent)

    agent = AgentHandle(agent_id="a", system_prompt="sys", model="m")
    await adapter.execute_with_tools(
        agent,
        "hello",
        _NullTool(),
        task_handle=handle,
        trace_id="trace-1",
    )

    msgs = sent[0]["messages"]
    assert all("<system_reminder>" not in (m.get("content") or "") for m in msgs)
    # And no reminder.emitted event is appended.
    assert not any(e.kind == "reminder.emitted" for e in handle.log.read_events())
