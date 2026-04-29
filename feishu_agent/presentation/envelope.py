"""`MessageEnvelope` + `ActionBinding` (spec 005 §3.3, T533).

The envelope is what a leaf formatter produces: a delivery-ready unit
(card dict or plain text) plus the action bindings the delivery layer
needs to remember (to hand off future ``card.action.trigger`` callbacks
to the right handler).

Field choices worth calling out:

* ``card`` is ``dict[str, Any] | None`` — we deliberately do not type
  it as a `TypedDict`. Cards come out of the ``cards.v2`` helpers
  which already enforce structure, and deeper typing would ossify the
  schema across a Feishu client version bump we can't control.
* ``idempotency_key`` is the sole primary key the delivery layer uses
  to decide ``send_card`` vs ``update_card``. Leaves must derive it
  from stable inputs (trace_id + fold_key + first seq), never from
  timestamps or random values.
* ``ttl_seconds`` is advisory — only ``pending_action`` / ``handoff``
  honor it today (spec §8 composer_pending_action_ttl_seconds).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from feishu_agent.presentation.kinds import MessageKind


@dataclass(frozen=True)
class ActionBinding:
    """One button/form binding tracked alongside a MessageEnvelope.

    The delivery layer does not execute the handler itself — the
    composer's ``action_router`` (spec §4.5, M2) owns the dispatch.
    Delivery only remembers these bindings so the ``action_router`` can
    look up the correct handler name from the incoming ``action_id``.
    """

    action_id: str  # format: "{kind}:{trace_id}:{name}" — see action_id.py (M1-I)
    handler_name: str
    ttl_seconds: int | None = None


@dataclass(frozen=True)
class MessageEnvelope:
    """A delivery-ready unit produced by a leaf formatter.

    Exactly one of ``card`` / ``text`` should be truthy in the normal
    path; both being None is permitted (leaf might produce nothing on a
    given frame — delivery treats that as a no-op).
    """

    kind: MessageKind
    idempotency_key: str
    card: dict[str, Any] | None = None
    text: str | None = None
    actions: tuple[ActionBinding, ...] = ()
    ttl_seconds: int | None = None

    @classmethod
    def text_only(
        cls,
        kind: MessageKind,
        text: str,
        idempotency_key: str,
        *,
        ttl_seconds: int | None = None,
    ) -> "MessageEnvelope":
        """Convenience constructor for plain-text envelopes.

        Used by leaves that never render as a card (``generic_text``)
        and by fallback paths in the delivery layer (``to_plain_text``
        round-trips through this to produce the emergency envelope
        when ``send_card`` errors out — see spec §4.4).
        """
        return cls(
            kind=kind,
            idempotency_key=idempotency_key,
            card=None,
            text=text,
            actions=(),
            ttl_seconds=ttl_seconds,
        )
