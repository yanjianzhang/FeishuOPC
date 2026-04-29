"""Tests for ``CancelToken`` + ``CancelTokenRegistry``.

Covers the cooperative-cancel contract the tool loop relies on:

- Construction outside a running loop is safe (lazy Event creation).
- ``cancel()`` is idempotent and the first reason wins.
- ``check()`` raises ``SessionCancelledError`` only after cancel.
- ``CancelTokenRegistry.cancel`` returns True / False depending on
  whether a matching session exists — the Feishu side uses this
  return value to decide between "OK 停了" and "没在跑".
"""

from __future__ import annotations

import asyncio

import pytest

from feishu_agent.core.cancel_token import (
    CancelKey,
    CancelToken,
    CancelTokenRegistry,
    SessionCancelledError,
)


def test_construct_outside_loop_is_safe():
    """Token must be constructable from sync code (tests / fixtures).

    We deliberately avoid creating an ``asyncio.Event`` at __init__
    time because that would bind to whatever loop happens to be
    current, which can bite badly in pytest-asyncio.
    """
    token = CancelToken()
    assert token.is_cancelled is False
    assert token.reason == ""


@pytest.mark.asyncio
async def test_cancel_sets_flag_and_check_raises():
    token = CancelToken()

    token.check()  # no-op pre-cancel

    token.cancel(reason="test")
    assert token.is_cancelled is True
    assert token.reason == "test"
    with pytest.raises(SessionCancelledError) as excinfo:
        token.check()
    assert "test" in str(excinfo.value)


@pytest.mark.asyncio
async def test_cancel_is_idempotent_first_reason_wins():
    """Calling cancel twice must not overwrite the original reason —
    we'd lose the operator's context (they called cancel with
    reason=\"user_requested\"; a later cancel=\"timeout\" from a watchdog
    shouldn't rewrite history)."""
    token = CancelToken()
    token.cancel(reason="first")
    token.cancel(reason="second")
    assert token.reason == "first"


@pytest.mark.asyncio
async def test_wait_unblocks_after_cancel():
    token = CancelToken()

    async def canceller():
        await asyncio.sleep(0)
        token.cancel(reason="late")

    # ``wait`` should unblock immediately after ``cancel`` fires.
    asyncio.create_task(canceller())
    await asyncio.wait_for(token.wait(), timeout=1.0)
    assert token.is_cancelled


def test_registry_cancel_returns_false_for_unknown():
    reg = CancelTokenRegistry()
    assert reg.cancel(CancelKey("bot", "chat", "thr")) is False


def test_registry_cancel_returns_true_and_cancels_token():
    reg = CancelTokenRegistry()
    key = CancelKey("bot", "chat", "thr")
    token = reg.register(key)
    assert reg.cancel(key, reason="op") is True
    assert token.is_cancelled
    assert token.reason == "op"


def test_registry_register_twice_replaces_token():
    """Re-registering an active key drops the old token. We log no
    warning — if the previous session is already finished, its token
    is moot; if it's still running, the new session overrides it.
    The caller is responsible for clearing keys when a session ends."""
    reg = CancelTokenRegistry()
    key = CancelKey("bot", "chat", "thr")
    first = reg.register(key)
    second = reg.register(key)
    assert first is not second
    # Cancel applies only to the currently-registered token.
    reg.cancel(key)
    assert second.is_cancelled
    assert not first.is_cancelled


def test_registry_clear_removes_token():
    reg = CancelTokenRegistry()
    key = CancelKey("bot", "chat", "thr")
    reg.register(key)
    reg.clear(key)
    assert reg.get(key) is None
    # Subsequent cancel returns False (no live session).
    assert reg.cancel(key) is False


def test_cancel_key_describe_stable():
    """``describe`` is used in logs; pin the format so future
    refactors can't silently change operator expectations."""
    key = CancelKey("tech_lead", "oc_1", "om_2")
    assert key.describe() == "tech_lead:oc_1/om_2"
