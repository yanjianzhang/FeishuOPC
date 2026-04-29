"""Unit tests for ``feishu_agent.presentation.kinds`` (T535).

These guard two kinds of drift:
* enum value strings leaking out as public keys (metrics labels, log
  lines, action_id prefixes) — renaming one is a breaking change
* default leaf mapping remaining total over ``EventKind``
"""

from __future__ import annotations

from feishu_agent.presentation.events import EventKind
from feishu_agent.presentation.kinds import (
    DEFAULT_LEAF_FOR_EVENT,
    DELIVERY_INTERNAL_KINDS,
    MessageKind,
)


def test_message_kind_values_are_stable():
    """Pin the string values. Renaming a kind requires a conscious
    migration (metrics / hook payloads / action_ids all embed it)."""
    assert {k.value for k in MessageKind} == {
        "tool_use_group",
        "thinking",
        "plan_approval",
        "pending_action",
        "progress_update",
        "handoff",
        "rate_limit",
        "final_answer",
        "error",
        "generic_text",
    }


def test_message_kind_is_string_enum():
    """``MessageKind.TOOL_USE_GROUP == "tool_use_group"`` must hold so
    metric labels and log lines can use the enum directly."""
    assert MessageKind.TOOL_USE_GROUP == "tool_use_group"


def test_delivery_internal_kinds_are_disjoint_from_default_map():
    """Kinds in ``DELIVERY_INTERNAL_KINDS`` must NOT appear as default
    target of any event — otherwise the "only reachable via fallback"
    contract is broken."""
    mapped_kinds = set(DEFAULT_LEAF_FOR_EVENT.values())
    assert DELIVERY_INTERNAL_KINDS.isdisjoint(mapped_kinds)


def test_default_leaf_covers_every_event_kind():
    """Contract: every ``EventKind`` literal member maps to a ``MessageKind``.

    If ``events.py`` adds a new literal without updating the mapping,
    this test fails — which is the single safety net preventing the
    composer from silently falling through to ``GENERIC_TEXT`` for new
    events (and losing card affordances)."""
    # EventKind is Literal[...] — extract args via typing.get_args
    from typing import get_args

    all_event_kinds = set(get_args(EventKind))
    mapped = set(DEFAULT_LEAF_FOR_EVENT.keys())
    missing = all_event_kinds - mapped
    assert not missing, f"EventKind(s) missing from DEFAULT_LEAF_FOR_EVENT: {missing}"


def test_every_non_internal_message_kind_is_reachable_by_default():
    """Inverse of the above: every non-internal ``MessageKind`` has at
    least one default event routing to it. If a kind becomes orphaned,
    it's likely dead code or missing a hook producer."""
    reachable = set(DEFAULT_LEAF_FOR_EVENT.values()) | DELIVERY_INTERNAL_KINDS
    all_kinds = set(MessageKind)
    orphans = all_kinds - reachable
    assert not orphans, (
        f"MessageKind(s) with no default event route and not flagged "
        f"as delivery-internal: {orphans}"
    )
