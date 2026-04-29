"""Typed runtime memory assembly for prompt construction.

This module does not introduce a new storage engine. Its job is to take
the existing FeishuOPC memory projections — project notes, recent-run
history, session summary, runtime baseline, and transient reminders —
and surface them through a single read model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from feishu_agent.team.agent_notes_service import (
    AgentNotesService,
    render_notes_for_prompt,
)
from feishu_agent.team.last_run_memory_service import (
    LastRunMemoryService,
    render_last_run_for_prompt,
)
from feishu_agent.team.reminder_bus import build_reminder_block_for_handle
from feishu_agent.team.session_summary_service import (
    SessionSummary,
    SessionSummaryService,
)


@dataclass(frozen=True)
class MemoryFragment:
    """One prompt-ready fragment from a specific memory layer."""

    kind: str
    scope: str
    content: str
    source: str | None = None
    priority: int = 0
    transient: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        return (self.content or "").strip()

    def is_empty(self) -> bool:
        return not self.render()


@dataclass(frozen=True)
class MemoryQueryContext:
    """Inputs required to assemble runtime memory."""

    role_name: str
    user_query: str
    project_id: str | None = None
    task_handle: Any = None
    notes_service: AgentNotesService | None = None
    last_run_service: LastRunMemoryService | None = None
    baseline_fragment: str = ""
    current_trace_id: str | None = None
    notes_limit: int = 20


@dataclass(frozen=True)
class MemoryAssembly:
    """Structured result of prompt-time memory retrieval."""

    durable_fragments: list[MemoryFragment] = field(default_factory=list)
    recent_run_fragment: MemoryFragment | None = None
    session_summary_fragment: MemoryFragment | None = None
    baseline_fragment: MemoryFragment | None = None
    transient_reminder_fragment: MemoryFragment | None = None

    def ordered_durable_fragments(self) -> list[MemoryFragment]:
        """Return the fragments that belong in the *system prompt*.

        Ordering: durable project memory → recent-run memory → session
        summary → runtime baseline. ``transient_reminder_fragment`` is
        intentionally excluded here: reminders are injected as a fresh
        ``user`` message per turn by the LLM adapter, not baked into the
        system prompt. Callers that need the reminder fragment should
        read :attr:`transient_reminder_fragment` directly.
        """

        out: list[MemoryFragment] = []
        out.extend(f for f in self.durable_fragments if not f.is_empty())
        for fragment in (
            self.recent_run_fragment,
            self.session_summary_fragment,
            self.baseline_fragment,
        ):
            if fragment is not None and not fragment.is_empty():
                out.append(fragment)
        return out

    def system_prompt_suffix(self) -> str:
        parts = [
            fragment.render() for fragment in self.ordered_durable_fragments()
        ]
        parts = [part for part in parts if part]
        return "\n\n".join(parts)


class MemoryAssembler:
    """Build a unified prompt-time view over existing memory sources."""

    def __init__(
        self,
        *,
        session_summary_service: SessionSummaryService | None = None,
    ) -> None:
        self._session_summary_service = (
            session_summary_service or SessionSummaryService()
        )

    def build(self, query_context: MemoryQueryContext) -> MemoryAssembly:
        session_summary = self._session_summary_service.build_for_handle(
            query_context.task_handle,
            exclude_trace_id=query_context.current_trace_id,
        )
        # Only trust ``current_mode`` as a real mode signal when the
        # session summary is non-empty. A fresh / missing session would
        # otherwise surface the default ``"act"`` and give every note
        # containing the literal token "act" an unearned relevance boost.
        task_mode = (
            session_summary.current_mode if not session_summary.is_empty() else None
        )
        durable = self._build_durable_fragments(
            query_context=query_context,
            session_summary=session_summary,
            task_mode=task_mode,
        )
        recent_run = self._build_recent_run_fragment(query_context)
        summary_fragment = self._build_session_summary_fragment(session_summary)
        baseline_fragment = self._build_baseline_fragment(query_context.baseline_fragment)
        transient = self.build_transient_reminder_fragment(query_context.task_handle)
        return MemoryAssembly(
            durable_fragments=durable,
            recent_run_fragment=recent_run,
            session_summary_fragment=summary_fragment,
            baseline_fragment=baseline_fragment,
            transient_reminder_fragment=transient,
        )

    def build_transient_reminder_fragment(
        self, task_handle: Any
    ) -> MemoryFragment | None:
        """Convenience delegate; prefer :func:`build_transient_reminder_fragment`
        from this module when a full assembler is not needed (the reminder
        path does not depend on any other memory layer)."""

        return build_transient_reminder_fragment(task_handle)

    def _build_durable_fragments(
        self,
        *,
        query_context: MemoryQueryContext,
        session_summary: SessionSummary,
        task_mode: str | None,
    ) -> list[MemoryFragment]:
        notes_service = query_context.notes_service
        if notes_service is None:
            return []
        if hasattr(notes_service, "select_for_prompt"):
            notes = notes_service.select_for_prompt(
                query=query_context.user_query,
                limit=query_context.notes_limit,
                role=query_context.role_name,
                task_mode=task_mode,
            )
        else:
            notes = notes_service.read_recent(limit=query_context.notes_limit)
        text = render_notes_for_prompt(notes)
        if not text:
            return []
        metadata: dict[str, Any] = {
            "selected_count": len(notes),
        }
        if task_mode:
            metadata["task_mode"] = task_mode
        if not session_summary.is_empty():
            metadata["session_summary_seq"] = session_summary.source_last_seq
        return [
            MemoryFragment(
                kind="durable_project_memory",
                scope="project",
                content=text,
                source=str(notes_service.notes_path),
                priority=10,
                metadata=metadata,
            )
        ]

    def _build_recent_run_fragment(
        self, query_context: MemoryQueryContext
    ) -> MemoryFragment | None:
        service = query_context.last_run_service
        if service is None or not service.enabled:
            return None
        digest = service.load_inject_target()
        if digest is None:
            return None
        text = render_last_run_for_prompt(digest)
        if not text:
            return None
        return MemoryFragment(
            kind="recent_run_memory",
            scope="project",
            content=text,
            source=str(service.history_path),
            priority=20,
            metadata={
                "trace_id": digest.trace_id,
                "stop_reason": digest.stop_reason,
                "ok": digest.ok,
            },
        )

    def _build_session_summary_fragment(
        self, summary: SessionSummary
    ) -> MemoryFragment | None:
        text = summary.render_for_prompt()
        if not text:
            return None
        return MemoryFragment(
            kind="session_summary",
            scope="thread",
            content=text,
            source="task_event_log",
            priority=30,
            metadata=summary.to_dict(),
        )

    def _build_baseline_fragment(self, baseline_fragment: str) -> MemoryFragment | None:
        text = (baseline_fragment or "").strip()
        if not text:
            return None
        return MemoryFragment(
            kind="runtime_baseline",
            scope="project",
            content=text,
            source="git_preflight",
            priority=40,
        )

def build_transient_reminder_fragment(
    task_handle: Any,
) -> MemoryFragment | None:
    """Build the per-turn reminder fragment for the given task handle.

    Intentionally side-effect-free and dependency-light so the LLM
    adapter can call it once per turn without constructing a whole
    :class:`MemoryAssembler` (which would also spin up a
    :class:`SessionSummaryService` this path never uses).
    """

    if task_handle is None:
        return None
    reminder_text, reminders = build_reminder_block_for_handle(task_handle)
    if not reminder_text:
        return None
    return MemoryFragment(
        kind="transient_reminder",
        scope="thread",
        content=reminder_text,
        source="task_event_log",
        priority=100,
        transient=True,
        metadata={
            "rule_ids": [r.rule_id for r in reminders],
            "count": len(reminders),
        },
    )


__all__ = [
    "MemoryAssembler",
    "MemoryAssembly",
    "MemoryFragment",
    "MemoryQueryContext",
    "build_transient_reminder_fragment",
]
