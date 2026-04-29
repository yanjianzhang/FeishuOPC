"""A tiny pub/sub event bus for plugin lifecycle hooks.

Rationale
---------
Today ``execute_with_tools`` and ``tech_lead_executor`` hard-wire three
cross-cutting concerns into the tool loop:

- **Thread updates** → Feishu push to keep the user informed.
- **Audit persistence** → JSON per ``trace_id`` for observability.
- **Tool-call observers** → ``on_tool_call`` callbacks for sub-agent
  tracing.

Every new cross-cutting feature (progress sync, token-cost alerting,
session lineage rendering) today means another ``if handler:`` branch
in the adapter. That's exactly the pattern Hermes Agent v2026.3.28
replaced with explicit lifecycle hooks. We copy the shape:

    hooks = HookBus()
    hooks.subscribe("pre_llm_call", my_auditor)
    hooks.subscribe("post_llm_call", my_token_cost_tracker)

Design choices
--------------
1. **Best-effort, never raise.** A subscriber raising does NOT break
   the tool loop. The handler's exception is logged and discarded.
   This mirrors the old ``on_tool_call`` observer contract.

2. **Sync or async handlers.** ``fire`` detects awaitables via
   ``inspect.iscoroutine`` — subscribers can be either. This matches
   how ``on_tool_call`` already worked, so migrating subscribers is a
   no-op.

3. **Event names are strings, not enums.** Enums would be safer but
   harder to extend. We accept typos vs. hard-fail on plugin churn.
   The known events are listed in ``KNOWN_EVENTS`` for documentation
   only; subscribing to an unknown event is allowed (it'll simply
   never fire).

4. **Ordering preserved.** Subscribers fire in registration order. If
   two subscribers need ordering guarantees, register them in that
   order. We deliberately don't expose a priority field — the YAGNI
   version solves 99% of cases without the footgun.

5. **No removal API in the first pass.** Subscribing is once-at-boot
   in our codebase; nothing dynamically unsubscribes. Can add
   ``unsubscribe`` when we find a use case.

Non-goals
---------
- Not a general message bus. No queues, no backpressure, no persistence.
- No event-name namespacing beyond convention.
- No typed payloads — each event documents its own ``payload`` shape.
"""

from __future__ import annotations

import inspect
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable, Union

logger = logging.getLogger(__name__)

# One handler signature: ``(event_name, payload) -> None | Awaitable[None]``.
# Callers can either ignore ``event_name`` (most do) or route on it
# (the rare case of one subscriber listening to several events).
HookHandler = Callable[[str, dict[str, Any]], Union[Awaitable[None], None]]

# ---------------------------------------------------------------------------
# Canonical event names. Listed here as a single source of truth so code
# review can flag typos. Subscribers are NOT required to use these — any
# string works — but adapters should stick to the list.
# ---------------------------------------------------------------------------
KNOWN_EVENTS: frozenset[str] = frozenset(
    {
        "on_session_start",   # payload: {"trace_id", "role", "query"}
        "on_session_end",     # payload: {"trace_id", "role", "result"}
        "pre_llm_call",       # payload: {"trace_id", "model", "turn", "messages_len"}
        "post_llm_call",      # payload: {"trace_id", "model", "turn", "usage", "stop_reason", "latency_ms"}
        "on_tool_call",       # payload: {"trace_id", "role", "tool_name", "args", "result", "duration_ms"}
        "on_sub_agent_spawn", # payload: {"parent_trace_id", "child_trace_id", "role"}
        "on_sub_agent_end",   # payload: {"parent_trace_id", "child_trace_id", "role", "ok"}
        "on_cancel_requested", # payload: {"trace_id", "source"}
    }
)


class HookBus:
    """In-process synchronous/asynchronous event dispatcher.

    One instance per request is the intended lifecycle — the bus is
    cheap to construct (empty dict) and scoping it per-request keeps
    subscribers from leaking across Feishu messages.

    Subscribers are keyed by event name. ``fire`` / ``afire`` iterates
    the subscribers in order, catching and logging any handler errors
    so a broken plugin can't take down the tool loop.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[HookHandler]] = defaultdict(list)

    # --- subscription -----------------------------------------------------

    def subscribe(self, event: str, handler: HookHandler) -> None:
        """Register ``handler`` to be called on every ``fire(event, …)``.

        No-op if ``handler`` is already registered for ``event`` — we
        dedupe by object identity because double-subscribing is almost
        always a wiring bug and we'd rather eat it than fire twice.
        """
        bucket = self._handlers[event]
        if handler not in bucket:
            bucket.append(handler)

    def subscribe_many(
        self,
        events: list[str],
        handler: HookHandler,
    ) -> None:
        """Register one handler across multiple events.

        Saves boilerplate for audit-style subscribers that want to see
        everything. The handler must inspect ``event_name`` to
        discriminate.
        """
        for event in events:
            self.subscribe(event, handler)

    def handler_count(self, event: str) -> int:
        """Return how many subscribers are registered to ``event``.

        Exposed so tests can assert wiring without peeking at
        ``_handlers`` directly.
        """
        return len(self._handlers.get(event, ()))

    # --- dispatch ---------------------------------------------------------

    async def afire(self, event: str, payload: dict[str, Any] | None = None) -> None:
        """Async-aware fire: awaits handlers that return awaitables.

        Prefer this in any async context; it's the only version that
        handles ``async def`` subscribers correctly. Uses isinstance
        checks on the return value rather than ``asyncio.iscoroutinefunction``
        so lambdas that return coroutines also work.
        """
        if event not in self._handlers:
            return
        payload = payload or {}
        for handler in list(self._handlers[event]):  # snapshot — handler may subscribe
            try:
                maybe = handler(event, payload)
            except Exception:
                logger.warning(
                    "hook handler raised for event=%s handler=%r",
                    event,
                    handler,
                    exc_info=True,
                )
                continue
            if inspect.isawaitable(maybe):
                try:
                    await maybe
                except Exception:
                    logger.warning(
                        "hook handler coroutine raised for event=%s handler=%r",
                        event,
                        handler,
                        exc_info=True,
                    )

    def fire(self, event: str, payload: dict[str, Any] | None = None) -> None:
        """Sync fire. Ignores async handlers (logs a warning).

        Use this from synchronous code paths only (tech_lead_executor
        currently does most of its thread updates synchronously). If you
        subscribe an async handler to an event that's only fired
        synchronously, its coroutine will be created and leaked — we
        warn explicitly so operators notice.
        """
        if event not in self._handlers:
            return
        payload = payload or {}
        for handler in list(self._handlers[event]):
            try:
                maybe = handler(event, payload)
            except Exception:
                logger.warning(
                    "hook handler raised for event=%s handler=%r",
                    event,
                    handler,
                    exc_info=True,
                )
                continue
            if inspect.isawaitable(maybe):
                # Close the coroutine to avoid "coroutine was never
                # awaited" warnings, and loudly warn so the subscriber
                # is fixed.
                try:
                    maybe.close()  # type: ignore[union-attr]
                except Exception:
                    pass
                logger.warning(
                    "hook handler for event=%s returned a coroutine but "
                    "was fired synchronously; use afire() instead. "
                    "handler=%r",
                    event,
                    handler,
                )


# A module-level null bus. Callers that don't want to plumb the bus all
# the way through can default to this — ``fire`` with no subscribers is
# just a dict lookup, so the cost is trivial.
NULL_BUS: HookBus = HookBus()
