"""Tests for :class:`RequestContext` and ``needs`` integration."""

from __future__ import annotations

import asyncio
from pathlib import Path

from feishu_agent.core.request_context import (
    CANONICAL_CONTEXT_KEYS,
    RequestContext,
    validate_needs,
)
from feishu_agent.team.task_event_log import TaskKey
from feishu_agent.team.task_service import TaskService
from feishu_agent.tools.tool_registry import ToolRegistry, tool


def test_as_dict_exposes_all_canonical_keys_with_none_defaults():
    ctx = RequestContext()
    as_dict = ctx.as_dict()
    for key in CANONICAL_CONTEXT_KEYS:
        assert key in as_dict, f"canonical key {key} missing"
        assert as_dict[key] is None


def test_extra_overrides_named_fields():
    ctx = RequestContext(task_id="from-field", extra={"task_id": "from-extra"})
    assert ctx.as_dict()["task_id"] == "from-extra"


def test_validate_needs_detects_unknown_keys():
    assert validate_needs(("task_id", "chat_id")) == []
    # "typo" and "nope" aren't in the canonical set — surface them.
    assert validate_needs(("typo", "chat_id", "nope")) == ["nope", "typo"]


def test_registry_injects_from_request_context(tmp_path: Path):
    reg = ToolRegistry()
    seen: dict = {}

    @tool(
        name="demo.use_ctx",
        description="",
        input_schema={
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
        },
        needs=("task_id", "trace_id", "thread_update_fn"),
        registry=reg,
    )
    async def _use_ctx(*, note: str, task_id, trace_id, thread_update_fn):
        seen["args"] = (note, task_id, trace_id, thread_update_fn)
        return {"ok": True}

    svc = TaskService(tasks_root=tmp_path)
    handle = svc.open_or_resume(TaskKey(bot_name="b", chat_id="c", root_id="r"))

    async def _update(msg: str) -> None:
        return None

    ctx = RequestContext(
        task_id=handle.meta.task_id,
        task_handle=handle,
        trace_id="T-123",
        thread_update_fn=_update,
    )
    executor = reg.build_executor(context=ctx.as_dict())
    result = asyncio.run(executor.execute_tool("demo.use_ctx", {"note": "hi"}))
    assert result == {"ok": True}

    args = seen["args"]
    assert args[0] == "hi"
    assert args[1] == handle.meta.task_id
    assert args[2] == "T-123"
    assert args[3] is _update


def test_input_schema_does_not_leak_needs():
    """The LLM-visible ``input_schema`` must never include ``needs`` keys."""
    reg = ToolRegistry()

    @tool(
        name="demo.secret_needs",
        description="",
        input_schema={
            "type": "object",
            "properties": {"public": {"type": "string"}},
            "required": ["public"],
        },
        needs=("task_id", "chat_id", "thread_update_fn"),
        registry=reg,
    )
    async def _f(*, public: str, task_id, chat_id, thread_update_fn):
        return {"ok": True}

    [spec] = reg.list_tools()
    # The schema properties only contain the LLM-visible ``public``
    # field; needs-injected params are NOT advertised.
    assert set(spec.spec.input_schema["properties"].keys()) == {"public"}
    assert spec.spec.needs == ("task_id", "chat_id", "thread_update_fn")
