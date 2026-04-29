"""`MessageKind` enum + default event → kind mapping (spec 005 §3.2, T532).

`MessageKind` is what a *delivered* Feishu message is; it determines
which leaf in ``messages/`` renders the envelope. Not every
``EventKind`` maps 1:1 — several events fold into ``TOOL_USE_GROUP`` —
so we centralise the default mapping here (``fold_policy`` consults
this table for "kind when rule 2 does not apply").

Adding a new value here is a contract change: write the new leaf, then
register it against the new kind, then update the fold policy if the
new kind interacts with grouping rules.
"""

from __future__ import annotations

from enum import Enum

from feishu_agent.presentation.events import EventKind


class MessageKind(str, Enum):
    TOOL_USE_GROUP = "tool_use_group"
    THINKING = "thinking"
    PLAN_APPROVAL = "plan_approval"
    PENDING_ACTION = "pending_action"
    PROGRESS_UPDATE = "progress_update"
    HANDOFF = "handoff"
    RATE_LIMIT = "rate_limit"
    FINAL_ANSWER = "final_answer"
    ERROR = "error"
    GENERIC_TEXT = "generic_text"


#: Default ``EventKind → MessageKind`` mapping, consulted by fold policy
#: rule "default grouping when no higher-priority rule matches".
#:
#: Notes:
#: * ``thinking_chunk`` maps to ``TOOL_USE_GROUP`` because thinking
#:   events that arrive interleaved with tool calls are absorbed into
#:   the group card's "思考" sub-panel (spec §4.3 rule 2). A standalone
#:   ``THINKING`` leaf exists for the rare case of thinking without any
#:   adjacent tool use — that branch is handled by fold policy, not
#:   this table.
#: * Every ``EventKind`` must appear here so the contract test
#:   (T536 in tasks.md) can enforce total coverage.
DEFAULT_LEAF_FOR_EVENT: dict[EventKind, MessageKind] = {
    "tool_use_started": MessageKind.TOOL_USE_GROUP,
    "tool_use_finished": MessageKind.TOOL_USE_GROUP,
    "thinking_chunk": MessageKind.TOOL_USE_GROUP,
    "plan_proposed": MessageKind.PLAN_APPROVAL,
    "progress_update": MessageKind.PROGRESS_UPDATE,
    "pending_action": MessageKind.PENDING_ACTION,
    "handoff_request": MessageKind.HANDOFF,
    "rate_limited": MessageKind.RATE_LIMIT,
    "final_answer": MessageKind.FINAL_ANSWER,
    "error": MessageKind.ERROR,
}

#: Message kinds that are *only* reachable via fold policy fallback,
#: not via the default event mapping. Kept explicit so the coverage
#: test (T536) can whitelist them without silently accepting drift.
DELIVERY_INTERNAL_KINDS: frozenset[MessageKind] = frozenset({
    MessageKind.GENERIC_TEXT,
    MessageKind.THINKING,  # only reached when thinking_chunk has no tool_use neighbours
})
