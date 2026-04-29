"""Thread-scoped session summary derived from the task event log.

Why this module exists
----------------------
FeishuOPC already has several useful memory projections:

- ``TaskState`` captures structured operating state (mode / plan / todos).
- ``ReminderBus`` turns that state into transient turn-time nudges.
- ``TailWindowCompressor`` can collapse older message history.

What is missing is a compact *session-level* summary that sits between
"raw transcript history" and "durable project memory". This service
builds that bridge from the append-only task event log so:

1. a new Feishu message can inherit the current thread state without
   replaying or re-describing the whole thread in prompt text;
2. context compression has a better source of truth than a purely
   message-local middle-turn outline; and
3. future post-turn consolidation can produce candidates from a stable,
   typed summary instead of scraping arbitrary chat text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from feishu_agent.team.task_event_log import TaskEvent
from feishu_agent.team.task_service import TaskHandle
from feishu_agent.team.task_state import TaskStateProjector


def _clip(text: str, *, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 2)].rstrip() + "..."


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


@dataclass(frozen=True)
class SessionSummary:
    """Compact representation of the current thread state."""

    task_id: str | None = None
    current_mode: str = "act"
    plan_title: str = ""
    summary_text: str = ""
    open_loops: list[str] = field(default_factory=list)
    recent_decisions: list[str] = field(default_factory=list)
    pending_blockers: list[str] = field(default_factory=list)
    last_user_message: str = ""
    last_assistant_message: str = ""
    compressions: int = 0
    source_last_seq: int = -1

    def is_empty(self) -> bool:
        return not any(
            [
                self.summary_text,
                self.open_loops,
                self.recent_decisions,
                self.pending_blockers,
                self.last_user_message,
                self.last_assistant_message,
                self.plan_title,
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "current_mode": self.current_mode,
            "plan_title": self.plan_title,
            "summary_text": self.summary_text,
            "open_loops": list(self.open_loops),
            "recent_decisions": list(self.recent_decisions),
            "pending_blockers": list(self.pending_blockers),
            "last_user_message": self.last_user_message,
            "last_assistant_message": self.last_assistant_message,
            "compressions": self.compressions,
            "source_last_seq": self.source_last_seq,
        }

    def render_for_prompt(self, *, header: str = "## Session summary") -> str:
        if self.is_empty():
            return ""
        lines = [
            header,
            "",
            (
                "State carried over from this thread. Prefer this summary over "
                "deep, compressed history when they disagree, and verify with "
                "tools before taking irreversible actions."
            ),
            "",
        ]
        if self.summary_text:
            lines.append(f"- Summary: {self.summary_text}")
        lines.append(f"- Current mode: `{self.current_mode or 'act'}`")
        if self.plan_title:
            lines.append(f"- Active plan: {self.plan_title}")
        if self.open_loops:
            lines.append("- Open loops:")
            lines.extend(f"  - {item}" for item in self.open_loops)
        if self.pending_blockers:
            lines.append("- Pending blockers:")
            lines.extend(f"  - {item}" for item in self.pending_blockers)
        if self.recent_decisions:
            lines.append("- Recent decisions:")
            lines.extend(f"  - {item}" for item in self.recent_decisions)
        if self.last_user_message:
            lines.append(f"- Latest user focus: {self.last_user_message}")
        if self.last_assistant_message:
            lines.append(f"- Latest assistant reply: {self.last_assistant_message}")
        if self.compressions:
            lines.append(f"- Prior compressions in this thread: {self.compressions}")
        lines.append("")
        return "\n".join(lines)

    def to_compression_text(self) -> str:
        parts: list[str] = []
        if self.summary_text:
            parts.append(self.summary_text)
        if self.open_loops:
            parts.append("Open loops: " + "; ".join(self.open_loops[:3]))
        if self.pending_blockers:
            parts.append("Blockers: " + "; ".join(self.pending_blockers[:3]))
        if self.recent_decisions:
            parts.append("Decisions: " + "; ".join(self.recent_decisions[:2]))
        if not parts:
            return ""
        return " ".join(parts)


class SessionSummaryService:
    """Builds :class:`SessionSummary` objects from task events."""

    MAX_MESSAGE_PREVIEW = 160
    MAX_OPEN_LOOPS = 4
    MAX_DECISIONS = 4
    MAX_BLOCKERS = 4

    def build(
        self,
        events: list[TaskEvent],
        *,
        exclude_trace_id: str | None = None,
    ) -> SessionSummary:
        filtered = self._filter_events(events, exclude_trace_id=exclude_trace_id)
        if not filtered:
            return SessionSummary()
        state = TaskStateProjector().project(filtered)
        last_user = self._latest_message(filtered, kind="message.inbound")
        last_assistant = self._latest_message(filtered, kind="message.outbound")
        open_loops = self._build_open_loops(state)
        blockers = self._build_blockers(state)
        decisions = self._build_decisions(state)
        summary_text = self._compose_summary_text(
            current_mode=state.mode,
            plan_title=state.plan.title,
            open_loops=open_loops,
            blockers=blockers,
            decisions=decisions,
            last_user_message=last_user,
        )
        return SessionSummary(
            task_id=filtered[-1].task_id,
            current_mode=state.mode,
            plan_title=state.plan.title,
            summary_text=summary_text,
            open_loops=open_loops,
            recent_decisions=decisions,
            pending_blockers=blockers,
            last_user_message=last_user,
            last_assistant_message=last_assistant,
            compressions=state.compressions,
            source_last_seq=filtered[-1].seq,
        )

    def build_for_handle(
        self,
        task_handle: TaskHandle | None,
        *,
        exclude_trace_id: str | None = None,
    ) -> SessionSummary:
        if task_handle is None:
            return SessionSummary()
        try:
            events = task_handle.log.read_events()
        except Exception:
            return SessionSummary(task_id=task_handle.task_id)
        return self.build(events, exclude_trace_id=exclude_trace_id)

    def _filter_events(
        self,
        events: list[TaskEvent],
        *,
        exclude_trace_id: str | None,
    ) -> list[TaskEvent]:
        if not exclude_trace_id:
            return list(events)
        filtered: list[TaskEvent] = []
        for event in events:
            if event.trace_id != exclude_trace_id:
                filtered.append(event)
                continue
            # Exclude the current session's direct request/response trail so
            # the prompt summary carries prior thread state rather than
            # duplicating the active user turn verbatim.
            if event.kind in {"message.inbound", "message.outbound"}:
                continue
            filtered.append(event)
        return filtered

    def _latest_message(self, events: list[TaskEvent], *, kind: str) -> str:
        for event in reversed(events):
            if event.kind != kind:
                continue
            payload = event.payload or {}
            text = str(
                payload.get("command_text")
                or payload.get("content_preview")
                or payload.get("message")
                or ""
            ).strip()
            if text:
                return _clip(text, limit=self.MAX_MESSAGE_PREVIEW)
        return ""

    def _build_open_loops(self, state) -> list[str]:
        items: list[str] = []
        for step in state.plan.steps:
            if step.status in {"in_progress", "blocked"}:
                detail = f"Plan step {step.index + 1}: {step.title} [{step.status}]"
                if step.note:
                    detail += f" ({_clip(str(step.note), limit=80)})"
                items.append(detail)
        for todo in state.todos.values():
            if todo.status not in {"done", "cancelled"}:
                items.append(
                    f"Todo {todo.id}: {_clip(todo.text, limit=100)} [{todo.status}]"
                )
        return _dedupe_keep_order(items)[: self.MAX_OPEN_LOOPS]

    def _build_blockers(self, state) -> list[str]:
        items: list[str] = []
        for pending_id, payload in state.pending_actions.items():
            action = str(payload.get("action_type") or payload.get("action") or "")
            if action:
                items.append(f"Pending confirmation {pending_id}: {action}")
            else:
                items.append(f"Pending confirmation {pending_id}")
        for health in state.tool_health.values():
            if health.online:
                continue
            detail = f"Tool {health.tool_name} offline"
            if health.last_error:
                detail += f": {_clip(health.last_error, limit=120)}"
            items.append(detail)
        return _dedupe_keep_order(items)[: self.MAX_BLOCKERS]

    def _build_decisions(self, state) -> list[str]:
        items: list[str] = []
        for note in state.notes:
            text = str(note.get("text") or "").strip()
            tags = {
                str(tag).strip().lower()
                for tag in (note.get("tags") or [])
                if str(tag).strip()
            }
            if not text:
                continue
            if tags.intersection(
                {"decision", "memory", "constraint", "convention", "gotcha"}
            ):
                items.append(_clip(text, limit=120))
        return _dedupe_keep_order(items)[-self.MAX_DECISIONS :]

    def _compose_summary_text(
        self,
        *,
        current_mode: str,
        plan_title: str,
        open_loops: list[str],
        blockers: list[str],
        decisions: list[str],
        last_user_message: str,
    ) -> str:
        parts: list[str] = []
        if last_user_message:
            parts.append(f"Latest thread focus: {last_user_message}")
        parts.append(f"Current mode is {current_mode or 'act'}.")
        if plan_title:
            parts.append(f"Active plan: {plan_title}.")
        if open_loops:
            parts.append(f"{len(open_loops)} open loop(s) remain.")
        if blockers:
            parts.append(f"{len(blockers)} blocker(s) are currently active.")
        if decisions:
            parts.append(f"{len(decisions)} recent durable-worthy decision(s) were noted.")
        return " ".join(parts).strip()


__all__ = [
    "SessionSummary",
    "SessionSummaryService",
]
