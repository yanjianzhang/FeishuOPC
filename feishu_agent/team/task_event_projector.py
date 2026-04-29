"""Bridge ``HookBus`` events into the per-task append-only event log.

M1 dual-write strategy
----------------------
``feishu_runtime_service`` already wires a number of ``HookBus``
subscribers (``RunDigestCollector``, ``lineage_audit``, audit-service
writers, etc.) that persist lossy projections of the session. M1
introduces the authoritative event log; the tool-loop inside
:class:`LlmAgentAdapter` already emits ``llm.*`` / ``tool.*`` /
``llm.compression`` events directly to the handle, but any other
``HookBus`` fire that happens *outside* that loop (for example the
``on_session_end`` event fired by callers in PM / dispatch paths)
would otherwise skip the log.

:class:`TaskEventProjector` plugs into the bus once and mirrors a
curated subset of events onto the task handle. It is the bridge that
lets legacy subscribers and the new event log co-exist during M1.

Only **session-level** events that are not already emitted by the
adapter's internal path are mirrored:

- ``on_session_start`` → ``task.meta.session_start`` (informational)
- ``on_session_end``   → ``task.meta.session_end``
- ``on_sub_agent_spawn`` / ``on_sub_agent_end`` → audit crumbs

We deliberately do NOT mirror ``pre_llm_call`` / ``post_llm_call`` /
``on_tool_call`` — those would double-log with the adapter's built-in
emissions.
"""

from __future__ import annotations

import logging
from typing import Any

from feishu_agent.core.hook_bus import HookBus
from feishu_agent.team.task_service import TaskHandle

logger = logging.getLogger(__name__)


# Session-scoped meta events. Kept outside :data:`KNOWN_EVENT_KINDS`
# because they are auxiliary to the core stream and a caller stripping
# the projector should not leave the log with "unknown kind" warnings.
_SESSION_EVENT_MAP = {
    "on_session_start": "task.meta.session_start",
    "on_session_end": "task.meta.session_end",
    "on_sub_agent_spawn": "task.meta.sub_agent_spawn",
    "on_sub_agent_end": "task.meta.sub_agent_end",
    "on_cancel_requested": "task.meta.cancel_requested",
}


class TaskEventProjector:
    """Subscribe to a :class:`HookBus` and mirror to a :class:`TaskHandle`."""

    def __init__(self, task_handle: TaskHandle) -> None:
        self._handle = task_handle

    def attach(self, bus: HookBus) -> None:
        for event_name in _SESSION_EVENT_MAP:
            bus.subscribe(event_name, self._on_event)

    async def _on_event(self, event: str, payload: dict[str, Any]) -> None:
        kind = _SESSION_EVENT_MAP.get(event)
        if kind is None:
            return
        try:
            self._handle.append(
                kind=kind,
                trace_id=str(payload.get("trace_id") or "") or None,
                payload={k: v for k, v in payload.items() if k != "trace_id"},
            )
        except Exception:  # pragma: no cover — projector must never raise
            logger.debug(
                "task event projector append failed event=%s", event, exc_info=True
            )


__all__ = ["TaskEventProjector"]
