"""Unit tests for ``HookBus``.

Keep these tests narrow — they exist to pin the bus behavior that
the adapter / lineage code relies on:

- Subscribers fire in registration order.
- Sync and async subscribers both work.
- A raising subscriber doesn't break the dispatch of others.
- Unknown events are no-ops.
- Double-subscribing dedupes by identity.
"""

from __future__ import annotations

import pytest

from feishu_agent.core.hook_bus import HookBus


@pytest.mark.asyncio
async def test_afire_invokes_subscriber_in_order():
    bus = HookBus()
    seen: list[str] = []

    def sync_a(event, payload):
        seen.append(f"a:{payload['v']}")

    async def async_b(event, payload):
        seen.append(f"b:{payload['v']}")

    bus.subscribe("evt", sync_a)
    bus.subscribe("evt", async_b)
    await bus.afire("evt", {"v": 1})

    assert seen == ["a:1", "b:1"]


@pytest.mark.asyncio
async def test_afire_swallows_subscriber_exception():
    """One raising subscriber must not block subsequent subscribers.

    This is the single most important property of the bus: plugins
    crashing in production absolutely cannot brick the tool loop.
    """
    bus = HookBus()
    seen: list[str] = []

    def explodes(event, payload):
        raise RuntimeError("plugin bug")

    def survives(event, payload):
        seen.append("ok")

    bus.subscribe("evt", explodes)
    bus.subscribe("evt", survives)
    await bus.afire("evt")

    assert seen == ["ok"]


@pytest.mark.asyncio
async def test_afire_swallows_async_subscriber_exception():
    bus = HookBus()
    seen: list[str] = []

    async def explodes(event, payload):
        raise RuntimeError("async bug")

    async def survives(event, payload):
        seen.append("ok")

    bus.subscribe("evt", explodes)
    bus.subscribe("evt", survives)
    await bus.afire("evt")

    assert seen == ["ok"]


@pytest.mark.asyncio
async def test_afire_unknown_event_is_noop():
    bus = HookBus()
    seen: list[str] = []
    bus.subscribe("a", lambda e, p: seen.append("a"))
    await bus.afire("b")
    assert seen == []


def test_subscribe_dedupes_same_handler():
    bus = HookBus()

    def h(event, payload):
        pass

    bus.subscribe("evt", h)
    bus.subscribe("evt", h)

    assert bus.handler_count("evt") == 1


def test_subscribe_many_registers_across_events():
    bus = HookBus()

    def h(event, payload):
        pass

    bus.subscribe_many(["a", "b", "c"], h)

    assert bus.handler_count("a") == 1
    assert bus.handler_count("b") == 1
    assert bus.handler_count("c") == 1


def test_sync_fire_warns_on_async_subscriber(caplog):
    """Calling ``fire`` (sync) on a bus that has async subscribers
    should log a warning, close the coroutine, and NOT raise.

    We don't assert the exact warning text — just that one was
    emitted and the coroutine was properly closed (otherwise pytest
    whines about 'coroutine was never awaited').
    """
    bus = HookBus()
    bus.subscribe("evt", _make_coro_subscriber())

    with caplog.at_level("WARNING"):
        bus.fire("evt", {"v": 1})

    assert any("fired synchronously" in rec.message for rec in caplog.records)


def _make_coro_subscriber():
    """Extracted factory so the closure over the coroutine isn't
    captured by the caplog fixture (avoids accidental warning
    suppression)."""
    async def handler(event, payload):
        return None

    return handler
