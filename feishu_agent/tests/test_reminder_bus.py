"""Tests for :class:`ReminderBus` rule evaluation.

What the tests lock in
----------------------
1. Each built-in rule fires iff the state predicate holds and is
   silent otherwise (no false positives — this matters because the
   LLM's context budget is finite).
2. ``render`` returns exactly one ``<system_reminder>`` block
   regardless of how many reminders fired, or empty string when none.
3. Rules can be registered / unregistered without mutating the
   default list (isolation across instances).
4. A rule that raises is swallowed so one bad rule can't break the
   entire bus.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from feishu_agent.team.reminder_bus import (
    DEFAULT_RULES,
    Reminder,
    ReminderBus,
    _CallableRule,
)
from feishu_agent.team.task_state import PlanDoc, TaskState, Todo, ToolHealth


def _state(**kwargs) -> TaskState:
    return TaskState(task_id="t", last_seq=0, **kwargs)


_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_plan_mode_rule_fires_only_in_plan() -> None:
    bus = ReminderBus()

    out_act = [r for r in bus.evaluate(_state(mode="act"), now=_NOW) if r.rule_id == "plan_mode"]
    assert out_act == []

    out_plan = [r for r in bus.evaluate(_state(mode="plan"), now=_NOW) if r.rule_id == "plan_mode"]
    assert len(out_plan) == 1
    assert out_plan[0].severity == "nudge"


def test_empty_plan_rule_fires_when_plan_unset() -> None:
    bus = ReminderBus()

    with_plan = _state(mode="plan", plan=PlanDoc(title="x"))
    assert not any(r.rule_id == "empty_plan" for r in bus.evaluate(with_plan, now=_NOW))

    without_plan = _state(mode="plan")
    assert any(r.rule_id == "empty_plan" for r in bus.evaluate(without_plan, now=_NOW))


def test_stale_todo_rule_thresholds_on_five_minutes() -> None:
    bus = ReminderBus()

    # Within threshold → silent.
    fresh = _state(
        todos={
            "a": Todo(
                id="a",
                text="recent",
                status="open",
                updated_at=(_NOW - timedelta(seconds=60)).isoformat(),
            )
        }
    )
    assert not any(r.rule_id == "stale_todos" for r in bus.evaluate(fresh, now=_NOW))

    # Over threshold → fires.
    stale = _state(
        todos={
            "a": Todo(
                id="a",
                text="stale",
                status="in_progress",
                updated_at=(_NOW - timedelta(minutes=10)).isoformat(),
            )
        }
    )
    out = [r for r in bus.evaluate(stale, now=_NOW) if r.rule_id == "stale_todos"]
    assert len(out) == 1
    assert out[0].detail["todo_id"] == "a"

    # Done todos are never stale.
    done = _state(
        todos={
            "a": Todo(
                id="a",
                text="done",
                status="done",
                updated_at=(_NOW - timedelta(minutes=60)).isoformat(),
            )
        }
    )
    assert not any(r.rule_id == "stale_todos" for r in bus.evaluate(done, now=_NOW))


def test_tool_offline_and_recovered_rules() -> None:
    bus = ReminderBus()

    offline_state = _state(
        tool_health={
            "git": ToolHealth(
                tool_name="git",
                online=False,
                last_transition_at=_NOW.isoformat(),
                last_error="auth failed",
            )
        }
    )
    offline = [r for r in bus.evaluate(offline_state, now=_NOW) if r.rule_id == "tool_offline"]
    assert len(offline) == 1

    # Recovered within 60s → nudge.
    recovered_state = _state(
        tool_health={
            "git": ToolHealth(
                tool_name="git",
                online=True,
                last_transition_at=(_NOW - timedelta(seconds=30)).isoformat(),
                last_error="auth failed",
            )
        }
    )
    recov = [r for r in bus.evaluate(recovered_state, now=_NOW) if r.rule_id == "tool_recovered"]
    assert len(recov) == 1

    # Recovered > 60s ago → silent.
    stale_recov_state = _state(
        tool_health={
            "git": ToolHealth(
                tool_name="git",
                online=True,
                last_transition_at=(_NOW - timedelta(seconds=600)).isoformat(),
                last_error="auth failed",
            )
        }
    )
    stale_recov = [
        r for r in bus.evaluate(stale_recov_state, now=_NOW) if r.rule_id == "tool_recovered"
    ]
    assert stale_recov == []


def test_compression_rule_is_silent_until_non_zero() -> None:
    bus = ReminderBus()

    assert not any(r.rule_id == "compression" for r in bus.evaluate(_state(), now=_NOW))

    out = [r for r in bus.evaluate(_state(compressions=2), now=_NOW) if r.rule_id == "compression"]
    assert len(out) == 1
    assert out[0].detail["count"] == 2


def test_pending_action_rule_lists_each_outstanding() -> None:
    bus = ReminderBus()

    state = _state(pending_actions={"p1": {"action": "push"}, "p2": {"action": "merge"}})
    out = [r for r in bus.evaluate(state, now=_NOW) if r.rule_id == "pending_action"]
    assert {r.key for r in out} == {"p1", "p2"}


def test_render_wraps_every_reminder_in_single_tag() -> None:
    bus = ReminderBus()
    reminders = [
        Reminder(rule_id="x", key="-", severity="nudge", message="a"),
        Reminder(rule_id="y", key="-", severity="warn", message="b"),
    ]
    text = bus.render(reminders)
    assert text.startswith("<system_reminder>")
    assert text.endswith("</system_reminder>")
    assert "- [nudge] a" in text
    assert "- [warn] b" in text

    # Empty reminders → empty string (callers can skip injection).
    assert bus.render([]) == ""


def test_register_and_unregister_isolates_instances() -> None:
    bus1 = ReminderBus()
    bus2 = ReminderBus()
    extra = _CallableRule(
        name="extra",
        fn=lambda _state, _now: [Reminder(rule_id="extra", key="-", severity="nudge", message="hi")],
    )
    bus1.register(extra)

    assert any(r.rule_id == "extra" for r in bus1.evaluate(_state(), now=_NOW))
    assert not any(r.rule_id == "extra" for r in bus2.evaluate(_state(), now=_NOW))

    bus1.unregister("extra")
    assert not any(r.rule_id == "extra" for r in bus1.evaluate(_state(), now=_NOW))


def test_failing_rule_does_not_break_bus() -> None:
    def _boom(_state, _now):
        raise RuntimeError("bad rule")

    bus = ReminderBus(rules=list(DEFAULT_RULES) + [_CallableRule(name="boom", fn=_boom)])
    # Evaluation still succeeds; other rules still fire when applicable.
    bus.evaluate(_state(mode="plan"), now=_NOW)
