"""`LeafFormatter` protocol + `LeafRegistry` (spec 005 §3.2, T534).

A leaf is the smallest unit that knows how to turn a bundle of
homogeneous ``OutputEvent`` s into a ``MessageEnvelope``. The composer
calls ``leaf.format(events, previous, brief)`` once per fold group per
flush cycle — it is the leaf's job to:

1. Build the card dict via ``feishu_agent.presentation.cards.v2``
   helpers (or return text only, for ``GENERIC_TEXT``).
2. Compute a stable ``idempotency_key`` so delivery can decide between
   ``send_card`` and ``update_card``.
3. Provide ``to_plain_text`` for the spec §4.4 fallback path — if
   ``send_card`` fails (client version too old, quota, malformed
   card), delivery re-sends as plain text.

Registry lives here (not in ``messages/__init__.py``) so tests can
import the registry without triggering every leaf's side-effect
registration — important during incremental rollout when some leaves
don't exist yet.
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from feishu_agent.presentation.envelope import MessageEnvelope
from feishu_agent.presentation.events import OutputEvent
from feishu_agent.presentation.kinds import MessageKind


@runtime_checkable
class LeafFormatter(Protocol):
    """Contract every ``messages/*.py`` leaf must satisfy.

    ``supports_update_in_place`` tells delivery whether a second
    envelope with the same ``idempotency_key`` should patch the
    existing message (``update_card``) or start a new one. Leaves that
    render cumulative views (tool_use_group, progress_update) set True;
    terminal leaves (final_answer, error) set False.
    """

    kind: MessageKind
    supports_update_in_place: bool

    def format(
        self,
        events: Sequence[OutputEvent],
        previous: MessageEnvelope | None,
        brief: bool,
    ) -> MessageEnvelope: ...

    def to_plain_text(self, envelope: MessageEnvelope) -> str: ...


class LeafRegistry:
    """Tiny registry keyed by ``MessageKind``.

    No thread safety — leaves register at import time (single-threaded
    module init); runtime lookups are read-only.
    """

    def __init__(self) -> None:
        self._leaves: dict[MessageKind, LeafFormatter] = {}

    def register(self, leaf: LeafFormatter) -> None:
        """Register a leaf. Raises ``ValueError`` on duplicate kind.

        Duplicate registration is almost always a bug (two modules
        claiming the same kind); raising early makes the conflict
        loud at import time instead of silent-last-wins at runtime.
        """
        if leaf.kind in self._leaves:
            existing = type(self._leaves[leaf.kind]).__name__
            incoming = type(leaf).__name__
            raise ValueError(
                f"MessageKind {leaf.kind.value!r} already registered by "
                f"{existing}; {incoming} cannot re-register."
            )
        self._leaves[leaf.kind] = leaf

    def get(self, kind: MessageKind) -> LeafFormatter | None:
        return self._leaves.get(kind)

    def all_kinds(self) -> set[MessageKind]:
        return set(self._leaves)

    def clear(self) -> None:
        """Drop every registered leaf — tests only."""
        self._leaves.clear()


#: Module-level singleton. Leaves call ``LEAF_REGISTRY.register(self)``
#: at import time; composer / tests call ``LEAF_REGISTRY.get(kind)``.
LEAF_REGISTRY = LeafRegistry()
