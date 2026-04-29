"""Self-state tools for the agent — appends events, not IO.

Why "self" tools exist
----------------------
Until M2, every tool the LLM could call had a side effect on the
external world (git, files, Feishu thread). The plan / todo /
mode-switch operations we want the agent to reason with explicitly
were buried in prompt text. That made them invisible to the audit
log and impossible to cite from a reminder ("you said you were in
plan mode 3 turns ago — still true?").

:class:`TaskStateExecutor` fixes that by exposing a small, typed
suite of tools whose only effect is to *append an event* to the
task log. Everything else — mode, plan doc, todos, notes — is a
projection the reminder bus reads back.

Contract
--------
- Each tool returns a compact ``{"ok": true, "event": {"kind", "seq",
  "ts"}}`` payload so the LLM can reference the action later.
- All writes go through ``TaskHandle.append`` — no filesystem, no
  thread update, no Feishu call. The physical split from
  ``WorldExecutor`` (M3) is already clean here; we only need to
  register them separately later.
- Input schemas are Pydantic-validated so malformed arguments are
  rejected early with a deterministic error message.

Non-goals
---------
- No "read" tools. Reminders will feed state back to the LLM; the
  LLM doesn't need to query its own plan.
- No ``remove_todo`` / ``reset_plan``. Append-only is a feature. The
  LLM marks things cancelled / done, never deletes.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.team.task_service import TaskHandle

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic input models — drive both validation and OpenAI JSON schema.
# ---------------------------------------------------------------------------


class _SetModeInput(BaseModel):
    mode: str = Field(
        ...,
        description="New mode. Canonical: 'plan' or 'act'. Free-form modes allowed.",
        min_length=1,
        max_length=32,
    )
    reason: str | None = Field(
        default=None, description="Optional one-line reason for the switch."
    )


class _PlanStepInput(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    status: str = Field(default="pending")
    note: str | None = None


class _SetPlanInput(BaseModel):
    title: str = Field(default="", max_length=200)
    summary: str = Field(default="", max_length=2000)
    steps: list[_PlanStepInput] = Field(default_factory=list, max_length=50)


class _UpdatePlanStepInput(BaseModel):
    index: int = Field(..., ge=0)
    status: str = Field(..., min_length=1, max_length=32)
    note: str | None = None


class _AddTodoInput(BaseModel):
    id: str | None = Field(
        default=None,
        description="Optional stable id. Generated if missing.",
        max_length=64,
    )
    text: str = Field(..., min_length=1, max_length=2000)
    note: str | None = None


class _UpdateTodoInput(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    status: str | None = None
    text: str | None = None
    note: str | None = None


class _MarkDoneInput(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)


class _NoteInput(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=8)


# ---------------------------------------------------------------------------
# Tool registry table
# ---------------------------------------------------------------------------


def _schema_of(model: type[BaseModel]) -> dict[str, Any]:
    """Return a JSON schema compatible with OpenAI function tools.

    We strip the Pydantic-specific ``title`` / ``$defs`` wrappers to
    keep the LLM's view slim.
    """
    schema = model.model_json_schema()
    schema.pop("title", None)
    return schema


_TOOL_REGISTRY: list[tuple[str, str, type[BaseModel]]] = [
    (
        "set_mode",
        "Switch the agent's cognitive mode (e.g. 'plan' → 'act').",
        _SetModeInput,
    ),
    (
        "set_plan",
        "Commit a structured plan document (title + steps) before entering act mode.",
        _SetPlanInput,
    ),
    (
        "update_plan_step",
        "Update the status/note of an existing plan step by index.",
        _UpdatePlanStepInput,
    ),
    (
        "add_todo",
        "Create an ad-hoc todo item referenced by a stable id.",
        _AddTodoInput,
    ),
    (
        "update_todo",
        "Update text/status/note on an existing todo.",
        _UpdateTodoInput,
    ),
    (
        "mark_todo_done",
        "Mark a todo as done.",
        _MarkDoneInput,
    ),
    (
        "note",
        "Record a free-form note in the task log (audit-only; no behavioral effect).",
        _NoteInput,
    ),
]


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class TaskStateExecutor:
    """Expose the self-state tools against a :class:`TaskHandle`.

    Not thread-safe on its own — the task handle's per-task asyncio
    lock (held by the adapter's tool loop) provides the needed
    serialization.
    """

    def __init__(self, task_handle: TaskHandle) -> None:
        self._handle = task_handle

    def tool_specs(self) -> list[AgentToolSpec]:
        return [
            AgentToolSpec(
                name=name,
                description=desc,
                input_schema=_schema_of(model),
            )
            for name, desc, model in _TOOL_REGISTRY
        ]

    async def execute_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        handler = _DISPATCH.get(tool_name)
        if handler is None:
            return {"ok": False, "error": f"UNKNOWN_TOOL: {tool_name}"}
        try:
            return handler(self._handle, arguments)
        except ValidationError as exc:
            return {"ok": False, "error": "INVALID_ARGUMENTS", "detail": exc.errors()}
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("TaskStateExecutor tool=%s failed", tool_name, exc_info=True)
            return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Handlers (pure functions of (handle, arguments) → result dict)
# ---------------------------------------------------------------------------


def _ack(event: Any) -> dict[str, Any]:
    return {
        "ok": True,
        "event": {"kind": event.kind, "seq": event.seq, "ts": event.ts},
    }


def _set_mode(handle: TaskHandle, args: dict[str, Any]) -> dict[str, Any]:
    payload = _SetModeInput.model_validate(args)
    event = handle.append(
        kind="state.mode_set",
        payload=payload.model_dump(exclude_none=True),
    )
    return _ack(event)


def _set_plan(handle: TaskHandle, args: dict[str, Any]) -> dict[str, Any]:
    payload = _SetPlanInput.model_validate(args)
    data = payload.model_dump()
    # The projector wants ``index`` pre-populated per step so the
    # ordering is explicit in the log and independent of list order.
    for i, step in enumerate(data.get("steps") or []):
        step["index"] = i
    event = handle.append(kind="state.plan_set", payload=data)
    return _ack(event)


def _update_plan_step(handle: TaskHandle, args: dict[str, Any]) -> dict[str, Any]:
    payload = _UpdatePlanStepInput.model_validate(args)
    event = handle.append(
        kind="state.plan_step_updated",
        payload=payload.model_dump(exclude_none=True),
    )
    return _ack(event)


def _add_todo(handle: TaskHandle, args: dict[str, Any]) -> dict[str, Any]:
    payload = _AddTodoInput.model_validate(args)
    data = payload.model_dump(exclude_none=True)
    tid = data.get("id") or f"todo-{uuid.uuid4().hex[:8]}"
    data["id"] = tid
    event = handle.append(kind="state.todo_added", payload=data)
    result = _ack(event)
    result["id"] = tid
    return result


def _update_todo(handle: TaskHandle, args: dict[str, Any]) -> dict[str, Any]:
    payload = _UpdateTodoInput.model_validate(args)
    event = handle.append(
        kind="state.todo_updated",
        payload=payload.model_dump(exclude_none=True),
    )
    return _ack(event)


def _mark_todo_done(handle: TaskHandle, args: dict[str, Any]) -> dict[str, Any]:
    payload = _MarkDoneInput.model_validate(args)
    event = handle.append(kind="state.todo_done", payload=payload.model_dump())
    return _ack(event)


def _note(handle: TaskHandle, args: dict[str, Any]) -> dict[str, Any]:
    payload = _NoteInput.model_validate(args)
    event = handle.append(kind="state.note_added", payload=payload.model_dump())
    return _ack(event)


_DISPATCH = {
    "set_mode": _set_mode,
    "set_plan": _set_plan,
    "update_plan_step": _update_plan_step,
    "add_todo": _add_todo,
    "update_todo": _update_todo,
    "mark_todo_done": _mark_todo_done,
    "note": _note,
}


__all__ = ["TaskStateExecutor"]
