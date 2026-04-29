"""Unit tests for ``feishu_agent.presentation.envelope`` (T535)."""

from __future__ import annotations

import pytest

from feishu_agent.presentation.envelope import ActionBinding, MessageEnvelope
from feishu_agent.presentation.kinds import MessageKind


def test_action_binding_is_frozen():
    b = ActionBinding(action_id="pending_action:t:confirm", handler_name="h")
    with pytest.raises(Exception):
        b.handler_name = "h2"  # type: ignore[misc]


def test_envelope_defaults():
    env = MessageEnvelope(
        kind=MessageKind.FINAL_ANSWER,
        idempotency_key="k1",
    )
    assert env.card is None
    assert env.text is None
    assert env.actions == ()
    assert env.ttl_seconds is None


def test_envelope_is_frozen():
    env = MessageEnvelope(kind=MessageKind.ERROR, idempotency_key="k")
    with pytest.raises(Exception):
        env.text = "oops"  # type: ignore[misc]


def test_text_only_constructor():
    env = MessageEnvelope.text_only(
        MessageKind.GENERIC_TEXT,
        "plain message",
        idempotency_key="k-plain",
    )
    assert env.card is None
    assert env.text == "plain message"
    assert env.actions == ()
    assert env.kind == MessageKind.GENERIC_TEXT


def test_text_only_preserves_ttl():
    env = MessageEnvelope.text_only(
        MessageKind.RATE_LIMIT,
        "rate limited",
        idempotency_key="k",
        ttl_seconds=60,
    )
    assert env.ttl_seconds == 60


def test_actions_tuple_immutable():
    """Actions is a tuple so frozen dataclass semantics hold (lists
    would be mutable through aliasing even on a frozen dataclass)."""
    bindings = (
        ActionBinding(action_id="pending_action:t:confirm", handler_name="confirm"),
        ActionBinding(action_id="pending_action:t:cancel", handler_name="cancel"),
    )
    env = MessageEnvelope(
        kind=MessageKind.PENDING_ACTION,
        idempotency_key="k",
        actions=bindings,
    )
    assert isinstance(env.actions, tuple)
    with pytest.raises(TypeError):
        env.actions[0] = ActionBinding(  # type: ignore[index]
            action_id="x:y:z", handler_name="z"
        )
