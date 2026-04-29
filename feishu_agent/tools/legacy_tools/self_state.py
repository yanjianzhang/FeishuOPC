"""Decorator-registered self-state tools.

This module is the canonical example of the M3 architecture:
- Tools are plain async functions.
- They declare their JSON schema and runtime needs via ``@tool(...)``.
- The registry auto-discovers them on ``autodiscover(...)``.
- ``needs=("task_handle",)`` causes the adapter to inject the
  per-session :class:`TaskHandle` at call time; the LLM never sees it.

Functionally this mirrors :class:`TaskStateExecutor` (which remains
in place for the M2 direct-instantiation path). The two co-exist
intentionally during the M3 transition; new tools should prefer
this decorator form.
"""

from __future__ import annotations

import uuid
from typing import Any

from feishu_agent.team.task_service import TaskHandle
from feishu_agent.tools.tool_registry import tool


def _ack(event) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    return {
        "ok": True,
        "event": {"kind": event.kind, "seq": event.seq, "ts": event.ts},
    }


# ---------------------------------------------------------------------------
# set_mode
# ---------------------------------------------------------------------------


_SET_MODE_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {
            "type": "string",
            "minLength": 1,
            "maxLength": 32,
            "description": "Canonical 'plan' or 'act'; free-form allowed.",
        },
        "reason": {
            "type": "string",
            "description": "Optional one-line reason for the switch.",
        },
    },
    "required": ["mode"],
    "additionalProperties": False,
}


@tool(
    name="set_mode",
    description="Switch the agent's cognitive mode (plan/act).",
    input_schema=_SET_MODE_SCHEMA,
    effect="self",
    target="self.mode",
    needs=("task_handle",),
)
async def set_mode(
    *, mode: str, reason: str | None = None, task_handle: TaskHandle | None
) -> dict[str, Any]:
    if task_handle is None:
        return {"ok": False, "error": "NO_TASK_HANDLE"}
    payload = {"mode": mode}
    if reason:
        payload["reason"] = reason
    event = task_handle.append(kind="state.mode_set", payload=payload)
    return _ack(event)


# ---------------------------------------------------------------------------
# set_plan
# ---------------------------------------------------------------------------


_STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "status": {"type": "string", "default": "pending"},
        "note": {"type": "string"},
    },
    "required": ["title"],
    "additionalProperties": False,
}

_SET_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": 200, "default": ""},
        "summary": {"type": "string", "maxLength": 2000, "default": ""},
        "steps": {
            "type": "array",
            "items": _STEP_SCHEMA,
            "maxItems": 50,
            "default": [],
        },
    },
    "additionalProperties": False,
}


@tool(
    name="set_plan",
    description="Commit a structured plan document before entering act mode.",
    input_schema=_SET_PLAN_SCHEMA,
    effect="self",
    target="self.plan",
    needs=("task_handle",),
)
async def set_plan(
    *,
    title: str = "",
    summary: str = "",
    steps: list[dict[str, Any]] | None = None,
    task_handle: TaskHandle | None,
) -> dict[str, Any]:
    if task_handle is None:
        return {"ok": False, "error": "NO_TASK_HANDLE"}
    raw_steps = steps or []
    # Auto-index so the projector can update steps by position.
    # Build a fresh list of copied dicts — the LLM-supplied dicts
    # may be reused by the caller, so we must not mutate them.
    indexed_steps = [{**step, "index": i} for i, step in enumerate(raw_steps)]
    payload = {"title": title, "summary": summary, "steps": indexed_steps}
    event = task_handle.append(kind="state.plan_set", payload=payload)
    return _ack(event)


# ---------------------------------------------------------------------------
# update_plan_step
# ---------------------------------------------------------------------------


_UPDATE_STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "index": {"type": "integer", "minimum": 0},
        "status": {"type": "string", "minLength": 1, "maxLength": 32},
        "note": {"type": "string"},
    },
    "required": ["index", "status"],
    "additionalProperties": False,
}


@tool(
    name="update_plan_step",
    description="Update the status / note of an existing plan step by index.",
    input_schema=_UPDATE_STEP_SCHEMA,
    effect="self",
    target="self.plan",
    needs=("task_handle",),
)
async def update_plan_step(
    *,
    index: int,
    status: str,
    note: str | None = None,
    task_handle: TaskHandle | None,
) -> dict[str, Any]:
    if task_handle is None:
        return {"ok": False, "error": "NO_TASK_HANDLE"}
    payload: dict[str, Any] = {"index": index, "status": status}
    if note is not None:
        payload["note"] = note
    event = task_handle.append(kind="state.plan_step_updated", payload=payload)
    return _ack(event)


# ---------------------------------------------------------------------------
# add_todo / update_todo / mark_todo_done
# ---------------------------------------------------------------------------


_ADD_TODO_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "maxLength": 64},
        "text": {"type": "string", "minLength": 1, "maxLength": 2000},
        "note": {"type": "string"},
    },
    "required": ["text"],
    "additionalProperties": False,
}


@tool(
    name="add_todo",
    description="Create an ad-hoc todo item referenced by a stable id.",
    input_schema=_ADD_TODO_SCHEMA,
    effect="self",
    target="self.todo",
    needs=("task_handle",),
)
async def add_todo(
    *,
    text: str,
    id: str | None = None,
    note: str | None = None,
    task_handle: TaskHandle | None,
) -> dict[str, Any]:
    if task_handle is None:
        return {"ok": False, "error": "NO_TASK_HANDLE"}
    tid = id or f"todo-{uuid.uuid4().hex[:8]}"
    payload: dict[str, Any] = {"id": tid, "text": text}
    if note is not None:
        payload["note"] = note
    event = task_handle.append(kind="state.todo_added", payload=payload)
    result = _ack(event)
    result["id"] = tid
    return result


_UPDATE_TODO_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 1, "maxLength": 64},
        "status": {"type": "string"},
        "text": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["id"],
    "additionalProperties": False,
}


@tool(
    name="update_todo",
    description="Update text / status / note on an existing todo.",
    input_schema=_UPDATE_TODO_SCHEMA,
    effect="self",
    target="self.todo",
    needs=("task_handle",),
)
async def update_todo(
    *,
    id: str,
    status: str | None = None,
    text: str | None = None,
    note: str | None = None,
    task_handle: TaskHandle | None,
) -> dict[str, Any]:
    if task_handle is None:
        return {"ok": False, "error": "NO_TASK_HANDLE"}
    payload: dict[str, Any] = {"id": id}
    if status is not None:
        payload["status"] = status
    if text is not None:
        payload["text"] = text
    if note is not None:
        payload["note"] = note
    event = task_handle.append(kind="state.todo_updated", payload=payload)
    return _ack(event)


_MARK_DONE_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 1, "maxLength": 64},
    },
    "required": ["id"],
    "additionalProperties": False,
}


@tool(
    name="mark_todo_done",
    description="Mark a todo as done.",
    input_schema=_MARK_DONE_SCHEMA,
    effect="self",
    target="self.todo",
    needs=("task_handle",),
)
async def mark_todo_done(
    *, id: str, task_handle: TaskHandle | None
) -> dict[str, Any]:
    if task_handle is None:
        return {"ok": False, "error": "NO_TASK_HANDLE"}
    event = task_handle.append(kind="state.todo_done", payload={"id": id})
    return _ack(event)


# ---------------------------------------------------------------------------
# note
# ---------------------------------------------------------------------------


_NOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": 2000},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 8,
            "default": [],
        },
    },
    "required": ["text"],
    "additionalProperties": False,
}


@tool(
    name="note",
    description="Record a free-form note in the task log (audit-only).",
    input_schema=_NOTE_SCHEMA,
    effect="self",
    target="self.note",
    needs=("task_handle",),
)
async def note(
    *,
    text: str,
    tags: list[str] | None = None,
    task_handle: TaskHandle | None,
) -> dict[str, Any]:
    if task_handle is None:
        return {"ok": False, "error": "NO_TASK_HANDLE"}
    event = task_handle.append(
        kind="state.note_added", payload={"text": text, "tags": tags or []}
    )
    return _ack(event)


__all__ = [
    "set_mode",
    "set_plan",
    "update_plan_step",
    "add_todo",
    "update_todo",
    "mark_todo_done",
    "note",
]
