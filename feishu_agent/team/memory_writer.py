"""Post-turn memory writer candidate generation.

V1 keeps this intentionally conservative: it does not mutate durable
memory automatically. Instead it derives candidate updates from the task
log and appends one structured event so operators (and future tooling)
can inspect what would be worth persisting.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from feishu_agent.team.agent_notes_service import AgentNotesService
from feishu_agent.team.last_run_memory_service import LastRunMemoryService
from feishu_agent.team.session_summary_service import (
    SessionSummary,
    SessionSummaryService,
)
from feishu_agent.team.task_service import TaskHandle

logger = logging.getLogger(__name__)

_MEMORY_CANDIDATE_TAGS = {
    "memory",
    "decision",
    "constraint",
    "convention",
    "gotcha",
}

# Bilingual markers for stale-candidate heuristics. Repo content is mixed
# zh/en so any English-only substring check misses most real notes. These
# sets are intentionally small and conservative — V1 prefers false-negative
# (miss a stale note) over false-positive (flag an active blocker as stale).
_BLOCKER_MARKERS = {
    "blocked",
    "blocker",
    "阻塞",
    "受阻",
    "挂起",
    "卡住",
}
_TODO_MARKERS = {
    "todo",
    "to-do",
    "fixme",
    "待办",
    "待处理",
    "遗留",
    "未完成",
}
# Explicit stale tags we recognize inside note text (e.g. "[blocked]" or
# "#待办"). Any note carrying one of these tokens as a tag-like marker is
# considered a stale candidate when the same marker is absent from the
# current thread's blockers / open loops.
_STALE_TAG_RE = re.compile(
    r"(?:^|\s|#|\[)(blocked|blocker|阻塞|受阻|挂起|卡住|todo|to-do|fixme|待办|待处理|遗留|未完成)(?:\]|\b|$)",
    re.IGNORECASE,
)


def _contains_any(text: str, markers: set[str]) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


def _markers_in_note(body: str) -> set[str]:
    """Return the lower-cased stale markers mentioned in a note body."""

    if not body:
        return set()
    return {m.group(1).lower() for m in _STALE_TAG_RE.finditer(body)}


@dataclass(frozen=True)
class MemoryWriteCandidates:
    add_note_candidates: list[str] = field(default_factory=list)
    stale_note_candidates: list[str] = field(default_factory=list)
    session_summary_update: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "add_note_candidates": list(self.add_note_candidates),
            "stale_note_candidates": list(self.stale_note_candidates),
            "session_summary_update": dict(self.session_summary_update),
        }


class MemoryWriterService:
    """Generate post-session memory candidates from runtime state."""

    EVENT_KIND = "memory.candidates_generated"

    def __init__(
        self,
        *,
        task_handle: TaskHandle | None,
        notes_service: AgentNotesService | None,
        last_run_service: LastRunMemoryService | None = None,
        session_summary_service: SessionSummaryService | None = None,
    ) -> None:
        self._task_handle = task_handle
        self._notes_service = notes_service
        self._last_run_service = last_run_service
        self._summary_service = session_summary_service or SessionSummaryService()

    def attach(self, bus) -> None:
        bus.subscribe("on_session_end", self._on_session_end)

    def generate_candidates(self) -> MemoryWriteCandidates:
        summary = self._summary_service.build_for_handle(self._task_handle)
        add_candidates = self._build_add_note_candidates()
        stale_candidates = self._build_stale_candidates(summary)
        return MemoryWriteCandidates(
            add_note_candidates=add_candidates,
            stale_note_candidates=stale_candidates,
            session_summary_update=summary.to_dict(),
        )

    async def _on_session_end(self, event: str, payload: dict[str, Any]) -> None:
        if self._task_handle is None:
            return
        try:
            candidates = self.generate_candidates()
            self._task_handle.append(
                kind=self.EVENT_KIND,
                trace_id=str(payload.get("trace_id") or "") or None,
                payload=candidates.to_payload(),
            )
        except Exception:  # pragma: no cover - must never break session end
            logger.warning("memory writer candidate generation failed", exc_info=True)

    def _build_add_note_candidates(self) -> list[str]:
        if self._task_handle is None:
            return []
        existing = {
            note.note.strip().lower()
            for note in (self._notes_service.read_recent(limit=50) if self._notes_service else [])
            if note.note.strip()
        }
        out: list[str] = []
        for event in self._task_handle.log.read_events():
            if event.kind != "state.note_added":
                continue
            text = str((event.payload or {}).get("text") or "").strip()
            tags = {
                str(tag).strip().lower()
                for tag in ((event.payload or {}).get("tags") or [])
                if str(tag).strip()
            }
            if not text or not tags.intersection(_MEMORY_CANDIDATE_TAGS):
                continue
            key = text.lower()
            if key in existing or key in {item.lower() for item in out}:
                continue
            out.append(text)
        return out[:5]

    def _build_stale_candidates(self, summary: SessionSummary) -> list[str]:
        """Surface notes whose "still unresolved?" marker no longer appears
        in the live session state.

        Inputs are AGENT_NOTES rows (``AgentNote``) — they do not carry
        structured tags; we therefore look for bilingual markers inside
        the note body and compare against the rendered open-loops /
        blocker text on the current :class:`SessionSummary`. Notes whose
        marker kind has "disappeared" from the live state are proposed
        as candidates for human-driven retirement.
        """

        if self._notes_service is None:
            return []
        recent_notes = self._notes_service.read_recent(limit=20)
        open_loop_text = " ".join(summary.open_loops)
        blocker_text = " ".join(summary.pending_blockers)
        summary_text = " ".join(
            [summary.summary_text, open_loop_text, blocker_text]
        )
        out: list[str] = []
        for note in recent_notes:
            body = note.note.strip()
            if not body:
                continue
            markers = _markers_in_note(body)
            note_has_blocker = bool(markers) and any(
                m in _BLOCKER_MARKERS for m in markers
            ) or _contains_any(body, _BLOCKER_MARKERS)
            note_has_todo = bool(markers) and any(
                m in _TODO_MARKERS for m in markers
            ) or _contains_any(body, _TODO_MARKERS)
            live_has_blocker = _contains_any(
                blocker_text, _BLOCKER_MARKERS
            ) or _contains_any(summary_text, _BLOCKER_MARKERS)
            live_has_todo = _contains_any(
                open_loop_text, _TODO_MARKERS
            ) or _contains_any(summary_text, _TODO_MARKERS)
            if note_has_blocker and not live_has_blocker:
                out.append(body)
            elif note_has_todo and not live_has_todo:
                out.append(body)
        return out[:3]


__all__ = [
    "MemoryWriteCandidates",
    "MemoryWriterService",
]
