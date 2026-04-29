"""Structured :class:`TaskState` that M2 projects from the event log.

What problem this solves
------------------------
M1 gave us an append-only event log and a lossy ``m1_lite`` snapshot
good enough for the CLI. M2 formalizes the agent's *mental state* —
mode (plan vs. act), plan document, todos, tool health, pending
confirmations — so we can:

1. **Reason about it explicitly**. The reminder bus (next module)
   inspects :class:`TaskState` to decide which ``<system_reminder>``
   blocks to inject. "Plan mode is on" / "this todo has been open for
   5 minutes with no update" / "tool X just came back online" — these
   all need a typed state, not a pile of dict payloads.
2. **Round-trip cleanly**. ``TaskState`` ↔ ``dict`` ↔ ``events``.
   Snapshot + events = new TaskState. Re-running replay after a crash
   must converge on the same object.
3. **Keep the event log canonical**. The fields here are projections,
   not storage. The only way to mutate state is to append an event
   and re-fold; :class:`TaskStateProjector` enforces that contract.

Design notes
------------
- **Frozen-ish records.** Dataclasses, not models. We deliberately
  don't use Pydantic — state lives in-process, cost matters, and the
  validation would have to be duplicated by the append contract
  anyway. Tests assert the shape.
- **Time is payload, not clock.** The projector never calls
  ``datetime.now()``. It reads ``event.ts`` so replay is
  deterministic on the same events regardless of when you replay.
- **Unknown kinds = no-op.** Extension without a central registry is
  a design goal; unknown ``kind`` values don't crash the projector.
- **One todo = one event-chain.** ``state.todo_added`` +
  ``state.todo_updated`` + ``state.todo_done`` all reference the
  same ``todo_id``; the projector reconciles by id, never by order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from feishu_agent.team.task_event_log import TaskEvent

# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


# Mode names. Kept as strings so md-based role packs can reference them
# without importing this module. Plan/act is the canonical split, but
# the type is open — free-form modes like "review" are fine.
Mode = str

_DEFAULT_MODE: Mode = "act"


@dataclass
class PlanStep:
    """One line-item in a plan document."""

    index: int
    title: str
    status: str = "pending"  # "pending" | "in_progress" | "done" | "blocked"
    note: str | None = None


@dataclass
class PlanDoc:
    """Static plan the agent commits to before entering act mode.

    A plan is optional — agents may skip planning and go straight to
    act. When present, it is *structured* (list of steps) rather than
    prose, because the reminder bus needs to cite specific steps
    ("step 2 has been in_progress for 10 minutes").
    """

    title: str = ""
    summary: str = ""
    steps: list[PlanStep] = field(default_factory=list)
    updated_at: str = ""

    def is_empty(self) -> bool:
        return not (self.title or self.summary or self.steps)


@dataclass
class Todo:
    """Ad-hoc todo item. Lifetime may outlive a single LLM turn.

    Distinct from :class:`PlanStep`: plan steps are committed up front
    and referenced by index; todos are created opportunistically mid-
    run ("note: the test file X needs cleanup before merge"). Both
    feed the same reminder rules.
    """

    id: str
    text: str
    status: str = "open"  # "open" | "in_progress" | "done" | "cancelled"
    created_at: str = ""
    updated_at: str = ""
    note: str | None = None


@dataclass
class ToolHealth:
    """Rolling health view for one tool.

    M2 only tracks a binary ``online`` flag + last transition
    timestamp. That's enough for the "tool X just came back online"
    reminder rule. Future iterations can add latency / error-rate
    windows.
    """

    tool_name: str
    online: bool = True
    last_transition_at: str = ""
    last_error: str | None = None


@dataclass
class TaskState:
    """Projection of the task event log.

    Every field is derived from events — never set directly. Callers
    wanting to mutate state must append the corresponding event and
    re-run the projector.
    """

    schema: str = "m2_taskstate"
    task_id: str | None = None
    last_seq: int = -1
    mode: Mode = _DEFAULT_MODE
    plan: PlanDoc = field(default_factory=PlanDoc)
    todos: dict[str, Todo] = field(default_factory=dict)
    tool_health: dict[str, ToolHealth] = field(default_factory=dict)
    notes: list[dict[str, Any]] = field(default_factory=list)
    pending_actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    compressions: int = 0

    # --- serialization ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "task_id": self.task_id,
            "last_seq": self.last_seq,
            "mode": self.mode,
            "plan": {
                "title": self.plan.title,
                "summary": self.plan.summary,
                "steps": [vars(s) for s in self.plan.steps],
                "updated_at": self.plan.updated_at,
            },
            "todos": {tid: vars(t) for tid, t in self.todos.items()},
            "tool_health": {
                name: vars(h) for name, h in self.tool_health.items()
            },
            "notes": list(self.notes),
            "pending_actions": {
                pid: dict(data) for pid, data in self.pending_actions.items()
            },
            "compressions": self.compressions,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TaskState":
        plan_raw = raw.get("plan") or {}
        plan = PlanDoc(
            title=str(plan_raw.get("title") or ""),
            summary=str(plan_raw.get("summary") or ""),
            steps=[
                PlanStep(
                    index=int(s.get("index") or 0),
                    title=str(s.get("title") or ""),
                    status=str(s.get("status") or "pending"),
                    note=s.get("note"),
                )
                for s in (plan_raw.get("steps") or [])
            ],
            updated_at=str(plan_raw.get("updated_at") or ""),
        )
        todos_raw = raw.get("todos") or {}
        todos = {
            tid: Todo(
                id=str(t.get("id") or tid),
                text=str(t.get("text") or ""),
                status=str(t.get("status") or "open"),
                created_at=str(t.get("created_at") or ""),
                updated_at=str(t.get("updated_at") or ""),
                note=t.get("note"),
            )
            for tid, t in todos_raw.items()
        }
        health_raw = raw.get("tool_health") or {}
        health = {
            name: ToolHealth(
                tool_name=str(h.get("tool_name") or name),
                online=bool(h.get("online", True)),
                last_transition_at=str(h.get("last_transition_at") or ""),
                last_error=h.get("last_error"),
            )
            for name, h in health_raw.items()
        }
        return cls(
            schema=str(raw.get("schema") or "m2_taskstate"),
            task_id=raw.get("task_id"),
            last_seq=int(raw.get("last_seq") or -1),
            mode=str(raw.get("mode") or _DEFAULT_MODE),
            plan=plan,
            todos=todos,
            tool_health=health,
            notes=list(raw.get("notes") or []),
            pending_actions=dict(raw.get("pending_actions") or {}),
            compressions=int(raw.get("compressions") or 0),
        )


# ---------------------------------------------------------------------------
# Projector
# ---------------------------------------------------------------------------


class TaskStateProjector:
    """Fold a stream of :class:`TaskEvent` into a :class:`TaskState`.

    The projector is a pure function wrapped in a class only so
    callers can subclass to extend ``kind`` handling without forking
    the base logic. All handlers here are small and name-prefixed so
    a subclass can override individual ``_on_<kind>`` methods.
    """

    def project(
        self,
        events: Iterable[TaskEvent],
        *,
        base: TaskState | None = None,
    ) -> TaskState:
        state = base or TaskState()
        for event in events:
            self._apply(state, event)
        return state

    def _apply(self, state: TaskState, event: TaskEvent) -> None:
        state.task_id = state.task_id or event.task_id
        if event.seq > state.last_seq:
            state.last_seq = event.seq
        payload = event.payload or {}
        handler = getattr(self, f"_on_{event.kind.replace('.', '_')}", None)
        if handler is None:
            return
        handler(state, event, payload)

    # --- lifecycle --------------------------------------------------------

    def _on_task_opened(self, state: TaskState, event: TaskEvent, payload: dict[str, Any]) -> None:
        # nothing beyond task_id is required at the state level.
        return None

    # --- mode / plan / todos ---------------------------------------------

    def _on_state_mode_set(self, state: TaskState, event: TaskEvent, payload: dict[str, Any]) -> None:
        mode = payload.get("mode")
        if isinstance(mode, str) and mode:
            state.mode = mode

    def _on_state_plan_set(self, state: TaskState, event: TaskEvent, payload: dict[str, Any]) -> None:
        steps_raw = payload.get("steps") or []
        steps: list[PlanStep] = []
        for idx, s in enumerate(steps_raw):
            if not isinstance(s, dict):
                continue
            steps.append(
                PlanStep(
                    index=int(s.get("index") or idx),
                    title=str(s.get("title") or ""),
                    status=str(s.get("status") or "pending"),
                    note=s.get("note"),
                )
            )
        state.plan = PlanDoc(
            title=str(payload.get("title") or state.plan.title),
            summary=str(payload.get("summary") or state.plan.summary),
            steps=steps or state.plan.steps,
            updated_at=event.ts,
        )

    def _on_state_plan_step_updated(
        self, state: TaskState, event: TaskEvent, payload: dict[str, Any]
    ) -> None:
        index = payload.get("index")
        if not isinstance(index, int):
            return
        for step in state.plan.steps:
            if step.index == index:
                status = payload.get("status")
                if isinstance(status, str) and status:
                    step.status = status
                note = payload.get("note")
                if note is not None:
                    step.note = str(note)
                state.plan.updated_at = event.ts
                return

    def _on_state_todo_added(
        self, state: TaskState, event: TaskEvent, payload: dict[str, Any]
    ) -> None:
        tid = str(payload.get("id") or "").strip()
        if not tid:
            return
        todo = Todo(
            id=tid,
            text=str(payload.get("text") or ""),
            status=str(payload.get("status") or "open"),
            created_at=event.ts,
            updated_at=event.ts,
            note=payload.get("note"),
        )
        state.todos[tid] = todo

    def _on_state_todo_updated(
        self, state: TaskState, event: TaskEvent, payload: dict[str, Any]
    ) -> None:
        tid = str(payload.get("id") or "").strip()
        todo = state.todos.get(tid)
        if todo is None:
            return
        status = payload.get("status")
        if isinstance(status, str) and status:
            todo.status = status
        text = payload.get("text")
        if isinstance(text, str):
            todo.text = text
        note = payload.get("note")
        if note is not None:
            todo.note = str(note)
        todo.updated_at = event.ts

    def _on_state_todo_done(
        self, state: TaskState, event: TaskEvent, payload: dict[str, Any]
    ) -> None:
        tid = str(payload.get("id") or "").strip()
        todo = state.todos.get(tid)
        if todo is None:
            return
        todo.status = "done"
        todo.updated_at = event.ts

    def _on_state_note_added(
        self, state: TaskState, event: TaskEvent, payload: dict[str, Any]
    ) -> None:
        state.notes.append({"ts": event.ts, **payload})

    # --- llm / tool health -----------------------------------------------

    def _on_llm_compression(
        self, state: TaskState, event: TaskEvent, payload: dict[str, Any]
    ) -> None:
        state.compressions += 1

    def _on_tool_call(
        self, state: TaskState, event: TaskEvent, payload: dict[str, Any]
    ) -> None:
        tool = str(payload.get("tool_name") or "").strip()
        if not tool:
            return
        health = state.tool_health.get(tool)
        if health is None:
            state.tool_health[tool] = ToolHealth(
                tool_name=tool,
                online=True,
                last_transition_at=event.ts,
            )

    def _on_tool_result(
        self, state: TaskState, event: TaskEvent, payload: dict[str, Any]
    ) -> None:
        tool = str(payload.get("tool_name") or "").strip()
        if not tool:
            return
        health = state.tool_health.setdefault(
            tool,
            ToolHealth(tool_name=tool, online=True, last_transition_at=event.ts),
        )
        if not health.online:
            health.online = True
            health.last_transition_at = event.ts
            # ``last_error`` is kept deliberately — the reminder bus
            # references it ("tool X recovered from: <error>"). Callers
            # that want a hard reset can emit a fresh ``tool.call``.

    def _on_tool_error(
        self, state: TaskState, event: TaskEvent, payload: dict[str, Any]
    ) -> None:
        tool = str(payload.get("tool_name") or "").strip()
        if not tool:
            return
        health = state.tool_health.setdefault(
            tool,
            ToolHealth(tool_name=tool, online=False, last_transition_at=event.ts),
        )
        # An error only toggles offline if the payload says so. The
        # reminder bus bias: be conservative about declaring outages.
        if payload.get("classify") == "offline":
            health.online = False
            health.last_transition_at = event.ts
        health.last_error = str(payload.get("error") or "")[:500]

    # --- pending / confirmations -----------------------------------------

    def _on_pending_requested(
        self, state: TaskState, event: TaskEvent, payload: dict[str, Any]
    ) -> None:
        pid = str(payload.get("pending_id") or payload.get("id") or "").strip()
        if not pid:
            return
        state.pending_actions[pid] = {"ts": event.ts, **payload}

    def _on_pending_resolved(
        self, state: TaskState, event: TaskEvent, payload: dict[str, Any]
    ) -> None:
        pid = str(payload.get("pending_id") or payload.get("id") or "").strip()
        state.pending_actions.pop(pid, None)


__all__ = [
    "Mode",
    "PlanStep",
    "PlanDoc",
    "Todo",
    "ToolHealth",
    "TaskState",
    "TaskStateProjector",
]
