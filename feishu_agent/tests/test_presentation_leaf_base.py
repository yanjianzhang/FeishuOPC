"""Unit tests for ``feishu_agent.presentation.messages._base`` (T535).

Covers the Protocol conformance + LeafRegistry contract. Real leaves
get their own tests in M1-B+; here we only need a stub to prove the
registry refuses duplicates and lookups work.
"""

from __future__ import annotations

from typing import Sequence

import pytest

from feishu_agent.presentation.envelope import MessageEnvelope
from feishu_agent.presentation.events import OutputEvent
from feishu_agent.presentation.kinds import MessageKind
from feishu_agent.presentation.messages._base import (
    LeafFormatter,
    LeafRegistry,
)


class _StubLeaf:
    """Minimal stub — just enough to satisfy ``LeafFormatter`` protocol."""

    def __init__(self, kind: MessageKind, *, supports_update: bool = False):
        self.kind = kind
        self.supports_update_in_place = supports_update

    def format(
        self,
        events: Sequence[OutputEvent],
        previous: MessageEnvelope | None,
        brief: bool,
    ) -> MessageEnvelope:
        return MessageEnvelope.text_only(self.kind, "stub", idempotency_key="k")

    def to_plain_text(self, envelope: MessageEnvelope) -> str:
        return envelope.text or ""


def test_stub_passes_protocol_isinstance_check():
    """``@runtime_checkable`` means ``isinstance(obj, LeafFormatter)``
    works — we rely on this in leaf registration for type safety."""
    leaf = _StubLeaf(MessageKind.GENERIC_TEXT)
    assert isinstance(leaf, LeafFormatter)


def test_registry_register_and_get():
    reg = LeafRegistry()
    leaf = _StubLeaf(MessageKind.FINAL_ANSWER)
    reg.register(leaf)
    assert reg.get(MessageKind.FINAL_ANSWER) is leaf


def test_registry_get_missing_returns_none():
    reg = LeafRegistry()
    assert reg.get(MessageKind.ERROR) is None


def test_registry_rejects_duplicate_kind():
    reg = LeafRegistry()
    reg.register(_StubLeaf(MessageKind.ERROR))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_StubLeaf(MessageKind.ERROR))


def test_registry_all_kinds_returns_copy():
    reg = LeafRegistry()
    reg.register(_StubLeaf(MessageKind.ERROR))
    kinds = reg.all_kinds()
    kinds.add(MessageKind.FINAL_ANSWER)
    # mutating the returned set must NOT affect registry state
    assert reg.all_kinds() == {MessageKind.ERROR}


def test_registry_clear_for_tests():
    reg = LeafRegistry()
    reg.register(_StubLeaf(MessageKind.ERROR))
    reg.clear()
    assert reg.all_kinds() == set()


def test_module_singleton_exposed_through_package():
    """Leaves import ``LEAF_REGISTRY`` from the package, not ``_base``
    — the re-export in ``__init__.py`` must return the same object."""
    from feishu_agent.presentation.messages import LEAF_REGISTRY as via_pkg
    from feishu_agent.presentation.messages._base import LEAF_REGISTRY as via_base

    assert via_pkg is via_base
