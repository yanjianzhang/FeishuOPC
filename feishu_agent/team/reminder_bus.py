"""Generate context-aware ``<system_reminder>`` blocks from :class:`TaskState`.

Motivation
----------
One monolithic system prompt cannot keep up with a long-running task.
As the conversation grows, the agent forgets:

- that it's still in plan mode and should not execute side effects,
- that a todo has been "in_progress" for 10 minutes without an update,
- that a previously broken tool just came back online,
- that the last context compression dropped N turns,
- that a world-side effect just landed (git push, CI failure).

``ReminderBus`` evaluates a small set of rules against the current
:class:`TaskState` and produces ``Reminder`` records. Each rule fires
at most one reminder per evaluation; the caller (the LLM adapter)
renders them inside a single ``<system_reminder>`` user message at
the top of the next turn.

Design choices
--------------
- **Pure function**. ``evaluate(state, now)`` returns a list;
  evaluation has no side effects. Tests pin the time parameter so
  "stale todo" checks are deterministic.
- **Rule objects, not a mega-function.** Each rule is a callable
  with a stable ``name`` so the LLM can reference a specific reminder
  ("the stale_todos reminder fired three turns ago — did you address
  it?") and so we can feature-flag individual rules in config later.
- **Severity + TTL.** Not all reminders matter for the next turn.
  ``severity="nudge"`` is a gentle hint ("plan is empty"); ``"block"``
  means "do not proceed without addressing this" (e.g. still in plan
  mode but the world tools are being invoked — M3 will use this).
- **Dedup by rule_id + key.** Two evaluations that both flag
  ``stale_todos[abc]`` emit a single record in the rendered tag; the
  LLM saw the same reminder last turn, we say "still stale" instead
  of spamming.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Protocol

from feishu_agent.team.task_state import TaskState

logger = logging.getLogger(__name__)


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        # Python 3.11+ parses ``+00:00`` offsets natively.
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Reminder record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reminder:
    """One line that will appear inside ``<system_reminder>``.

    ``rule_id`` and ``key`` together form a stable identity across
    turns; the adapter uses it to decide whether to render
    ("still stale") or suppress a repeat.
    """

    rule_id: str
    key: str  # e.g. todo id, tool name, or "-" for singleton rules
    severity: str  # "nudge" | "warn" | "block"
    message: str
    detail: dict[str, object] = field(default_factory=dict)

    def render(self) -> str:
        return f"- [{self.severity}] {self.message}"


# ---------------------------------------------------------------------------
# Rule protocol
# ---------------------------------------------------------------------------


class Rule(Protocol):
    """A rule is a plain callable; the protocol exists for typing only."""

    name: str

    def __call__(self, state: TaskState, now: datetime) -> list[Reminder]:
        ...


@dataclass
class _CallableRule:
    """Adapt a plain function to the :class:`Rule` protocol."""

    name: str
    fn: Callable[[TaskState, datetime], list[Reminder]]

    def __call__(self, state: TaskState, now: datetime) -> list[Reminder]:
        return self.fn(state, now)


# ---------------------------------------------------------------------------
# Built-in rules
# ---------------------------------------------------------------------------


def _rule_plan_mode(state: TaskState, now: datetime) -> list[Reminder]:
    """Remind the LLM that it is in plan mode.

    Fires whenever ``mode == "plan"``. Safe to spam — the LLM's tool
    policy should be filtering out world-effecting tools in plan
    mode already; the reminder is a belt-and-suspenders hint.
    """
    if state.mode != "plan":
        return []
    return [
        Reminder(
            rule_id="plan_mode",
            key="-",
            severity="nudge",
            message=(
                "You are in plan mode. Produce a plan before switching to act; "
                "world-effecting tools are not available until you call set_mode(mode='act')."
            ),
        )
    ]


def _rule_empty_plan(state: TaskState, now: datetime) -> list[Reminder]:
    """Nudge the LLM to commit a plan the first time it enters plan mode."""
    if state.mode != "plan":
        return []
    if not state.plan.is_empty():
        return []
    return [
        Reminder(
            rule_id="empty_plan",
            key="-",
            severity="nudge",
            message="You are in plan mode but haven't called set_plan yet.",
        )
    ]


def _stale_threshold_seconds() -> float:
    # Kept as a helper so a future config hook can override without
    # changing callers.
    return 5 * 60.0


def _rule_stale_todos(state: TaskState, now: datetime) -> list[Reminder]:
    """Flag todos that have been open > 5 minutes without an update."""
    threshold = _stale_threshold_seconds()
    reminders: list[Reminder] = []
    for todo in state.todos.values():
        if todo.status in {"done", "cancelled"}:
            continue
        last = _parse_ts(todo.updated_at or todo.created_at)
        if last is None:
            continue
        age = (now - last).total_seconds()
        if age < threshold:
            continue
        reminders.append(
            Reminder(
                rule_id="stale_todos",
                key=todo.id,
                severity="warn",
                message=f"Todo '{todo.text[:60]}' has been {todo.status} for {int(age // 60)} min.",
                detail={"todo_id": todo.id, "age_seconds": int(age)},
            )
        )
    return reminders


def _rule_tool_offline(state: TaskState, now: datetime) -> list[Reminder]:
    """Warn when a registered tool is offline."""
    out: list[Reminder] = []
    for name, health in state.tool_health.items():
        if health.online:
            continue
        out.append(
            Reminder(
                rule_id="tool_offline",
                key=name,
                severity="warn",
                message=f"Tool '{name}' is offline; avoid invoking it until it recovers.",
                detail={"tool_name": name, "last_error": health.last_error},
            )
        )
    return out


def _rule_tool_recovered(state: TaskState, now: datetime) -> list[Reminder]:
    """Note recent recoveries for one turn.

    We define "recent" as ``last_transition_at`` within the last 60s
    AND ``online=True`` AND a non-empty ``last_error``. That last
    condition is what tells us the recovery was a genuine flip
    (offline → online), not just a fresh ``tool.call``.
    """
    out: list[Reminder] = []
    for name, health in state.tool_health.items():
        if not health.online or not health.last_error:
            continue
        last = _parse_ts(health.last_transition_at)
        if last is None:
            continue
        if (now - last).total_seconds() > 60:
            continue
        out.append(
            Reminder(
                rule_id="tool_recovered",
                key=name,
                severity="nudge",
                message=(
                    f"Tool '{name}' recovered from: {health.last_error[:120]}. "
                    "You may resume using it."
                ),
                detail={"tool_name": name},
            )
        )
    return out


def _rule_compression(state: TaskState, now: datetime) -> list[Reminder]:
    """Tell the LLM how much context we have discarded.

    Even though the adapter handles the mechanics, the LLM benefits
    from the signal ("you've lost N blocks of older turns; don't rely
    on specific quotes from deep history").
    """
    if state.compressions == 0:
        return []
    return [
        Reminder(
            rule_id="compression",
            key="-",
            severity="nudge",
            message=(
                f"Context has been compressed {state.compressions} time(s); older turns "
                "may be summarized rather than verbatim. Verify details via tool calls if unsure."
            ),
            detail={"count": state.compressions},
        )
    ]


def _rule_pending_actions(state: TaskState, now: datetime) -> list[Reminder]:
    """Remind the LLM about outstanding confirmation prompts."""
    out: list[Reminder] = []
    for pid, data in state.pending_actions.items():
        out.append(
            Reminder(
                rule_id="pending_action",
                key=pid,
                severity="warn",
                message=(
                    f"Pending confirmation '{pid}' is still unresolved "
                    "(the user has not approved / rejected)."
                ),
                detail=dict(data),
            )
        )
    return out


DEFAULT_RULES: list[Rule] = [
    _CallableRule(name="plan_mode", fn=_rule_plan_mode),
    _CallableRule(name="empty_plan", fn=_rule_empty_plan),
    _CallableRule(name="stale_todos", fn=_rule_stale_todos),
    _CallableRule(name="tool_offline", fn=_rule_tool_offline),
    _CallableRule(name="tool_recovered", fn=_rule_tool_recovered),
    _CallableRule(name="compression", fn=_rule_compression),
    _CallableRule(name="pending_action", fn=_rule_pending_actions),
]


# ---------------------------------------------------------------------------
# ReminderBus
# ---------------------------------------------------------------------------


class ReminderBus:
    """Evaluate all rules against a :class:`TaskState`.

    Not a pub/sub bus in the classic sense — the name mirrors the
    plan document and emphasizes that rules are pluggable. Rules
    compose by addition; ``register`` appends, ``unregister`` removes
    by ``name``.
    """

    def __init__(self, rules: Iterable[Rule] | None = None) -> None:
        self._rules: list[Rule] = list(rules or DEFAULT_RULES)

    def register(self, rule: Rule) -> None:
        self._rules.append(rule)

    def unregister(self, name: str) -> None:
        self._rules = [r for r in self._rules if r.name != name]

    def evaluate(
        self,
        state: TaskState,
        *,
        now: datetime | None = None,
    ) -> list[Reminder]:
        now = now or _utcnow()
        out: list[Reminder] = []
        for rule in self._rules:
            try:
                out.extend(rule(state, now))
            except Exception:  # pragma: no cover — defensive
                # A bad rule must not take down the tool loop, but it
                # also must not vanish — log with traceback so
                # operators see misbehaving rules in the agent log.
                logger.exception(
                    "reminder rule %r raised; skipping this evaluation",
                    getattr(rule, "name", repr(rule)),
                )
                continue
        return out

    def render(
        self,
        reminders: Iterable[Reminder],
        *,
        tag: str = "system_reminder",
    ) -> str:
        """Render reminders into a single ``<system_reminder>`` block.

        Returns an empty string when there are no reminders so callers
        can cheaply avoid injecting a message. The shape mirrors the
        Cursor / Claude ``<system_reminder>`` convention: one tag, one
        bullet per reminder, severity prefix.
        """
        items = list(reminders)
        if not items:
            return ""
        body = "\n".join(r.render() for r in items)
        return f"<{tag}>\n{body}\n</{tag}>"


def build_reminder_block_for_handle(
    task_handle,  # noqa: ANN001 — typed in-body to avoid circular imports
    *,
    bus: ReminderBus | None = None,
    now: datetime | None = None,
) -> tuple[str, list[Reminder]]:
    """Project the task log to state and render reminders in one step.

    Returns ``(rendered_text, reminders)``. ``rendered_text`` is empty
    when no rules fire — callers should treat that as "no injection".

    This helper is intentionally independent of the adapter so tests
    can drive it directly and so the M3 ``CombinedExecutor`` can reuse
    it without dragging in any tool-loop machinery.
    """
    # Local import: keeps ``reminder_bus`` dependency-free at module load.
    from feishu_agent.team.task_state import TaskStateProjector

    if task_handle is None:
        return "", []
    try:
        events = task_handle.log.read_events()
    except Exception:  # pragma: no cover — defensive
        return "", []
    state = TaskStateProjector().project(events)
    bus = bus or ReminderBus()
    reminders = bus.evaluate(state, now=now)
    return bus.render(reminders), reminders


__all__ = [
    "Reminder",
    "Rule",
    "ReminderBus",
    "DEFAULT_RULES",
    "build_reminder_block_for_handle",
]
