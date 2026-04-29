"""Cooperative cancellation for agent tool loops.

Problem being solved
--------------------
``LlmAgentAdapter.execute_with_tools`` sits in ``for turn in range(max)``
awaiting a ``/chat/completions`` response plus any tool dispatch. If a
Feishu user realizes the tech-lead agent is off-task, they currently
have to wait out the full ``role_agent_timeout_seconds`` (7 minutes by
default) before the loop gives up.

We need a **cooperative** cancel — one that doesn't kill the HTTP
request mid-flight (would leak connections) but does stop the loop at
defined safe points so the user sees "停了" within one turn.

Design
------
- ``CancelToken`` wraps an ``asyncio.Event``. ``cancel()`` sets it;
  ``is_cancelled`` and ``check()`` test it.
- Safe checkpoints in the adapter:
    1. At the top of every turn (before sending ``/chat/completions``).
    2. After a tool call returns, before running the next one.
    3. After provider-pool failover (so a long retry chain is cancelable).
- ``CancelTokenRegistry`` maps a "conversation key" (bot + chat + root
  thread) to the active token, so a cancel request coming in over
  Feishu can find the right session. Registration is best-effort —
  if the user issues ``@bot 取消`` and no session is registered, we
  return a friendly "没在跑" rather than a crash.
- Cancellation raises ``SessionCancelledError`` from the checkpoint;
  the adapter catches it and returns a ``LlmSessionResult`` with
  ``stop_reason="cancelled"``.

Why not ``asyncio.CancelledError``?
-----------------------------------
That's asyncio's kill signal. Using it would tear down pending
``httpx`` requests and leak TLS/pool state. We want a graceful stop,
not a force-abort.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


class SessionCancelledError(Exception):
    """Raised at cancellation checkpoints inside the tool loop.

    Carries the reason so the user-facing message can explain *why*
    we stopped (user-initiated vs. operator kill vs. timeout shim).
    """

    def __init__(self, reason: str = "cancelled") -> None:
        super().__init__(reason)
        self.reason = reason


class CancelToken:
    """A cooperative cancellation flag shared across the tool loop.

    Idempotent: calling ``cancel()`` twice is fine (second call is a
    no-op). Thread-safe because the underlying ``asyncio.Event`` is
    bound to a specific loop; we acquire it lazily on first access so
    the token can be created from synchronous code and resolved later
    in the async loop.

    The object is intentionally tiny — no observer registration, no
    reason-stack. If you need more, build a service on top; don't
    overload the token.
    """

    def __init__(self) -> None:
        # ``asyncio.Event`` binds to the current loop at construction.
        # Tokens are created in async context in production, but tests
        # sometimes create them synchronously — defer binding.
        self._event: asyncio.Event | None = None
        self._reason: str = ""
        self._lock = threading.Lock()  # guards _reason / _event creation

    def _ensure_event(self) -> asyncio.Event:
        # ``asyncio.Event`` must be constructed inside a running loop
        # (Python 3.10+). Lazy-init keeps the token safe to construct
        # from synchronous fixtures.
        with self._lock:
            if self._event is None:
                self._event = asyncio.Event()
            return self._event

    def cancel(self, reason: str = "user_requested") -> None:
        """Set the cancel flag. Safe to call from any thread / loop."""
        ev = self._ensure_event()
        with self._lock:
            if self._reason == "":
                self._reason = reason
        ev.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event is not None and self._event.is_set()

    @property
    def reason(self) -> str:
        return self._reason

    def check(self) -> None:
        """Raise ``SessionCancelledError`` if cancellation was requested.

        Call this at every safe checkpoint in the tool loop. Cheap —
        one attribute access when not cancelled.
        """
        if self.is_cancelled:
            raise SessionCancelledError(self._reason or "cancelled")

    async def wait(self) -> None:
        """Block until cancelled. Useful for select-like patterns.

        Rarely used — the common pattern is ``check()`` at safe
        points — but offered for future use (e.g., a watchdog task
        that wants to short-circuit a running subprocess).
        """
        await self._ensure_event().wait()


# ---------------------------------------------------------------------------
# Registry — maps a conversation key to its live token so incoming
# ``@bot 取消`` messages can find the running session.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CancelKey:
    """Conversation-level identity of a running tool loop.

    Two independent tool loops MUST produce distinct keys, or one
    user's cancel would stop someone else's session. We compose a key
    from (bot_name, chat_id, root_thread_id) — the same tuple that
    Feishu thread updates key off of, so the mapping is obvious.
    """

    bot_name: str
    chat_id: str
    thread_id: str  # root_id; falls back to message_id when no thread.

    def describe(self) -> str:
        """Short human-friendly string for logging / thread updates."""
        return f"{self.bot_name}:{self.chat_id}/{self.thread_id}"


class CancelTokenRegistry:
    """In-process map from ``CancelKey`` to the active ``CancelToken``.

    Single process / single worker: a dict. Multi-process / multi-host
    deployments would need a redis-backed impl, but that's a future
    concern — Feishu's per-app concurrency is modest.

    Tokens auto-clear on session end via ``clear(key)``. If a session
    crashes without clearing, the next session registering the same
    key overwrites it — stale tokens just become garbage-collected,
    which is fine because ``cancel()`` on a finished loop is a no-op.
    """

    def __init__(self) -> None:
        self._tokens: dict[CancelKey, CancelToken] = {}
        self._lock = threading.Lock()

    def register(self, key: CancelKey) -> CancelToken:
        """Create or replace the token for ``key`` and return it.

        Returns a fresh token every time. If a previous token still
        exists, it's discarded (the old session is either done or
        the caller must have already cleaned up).
        """
        token = CancelToken()
        with self._lock:
            self._tokens[key] = token
        return token

    def get(self, key: CancelKey) -> Optional[CancelToken]:
        with self._lock:
            return self._tokens.get(key)

    def clear(self, key: CancelKey) -> None:
        """Remove the token for ``key``. Idempotent."""
        with self._lock:
            self._tokens.pop(key, None)

    def cancel(self, key: CancelKey, reason: str = "user_requested") -> bool:
        """Find and cancel the token for ``key``.

        Returns True if a token existed and was cancelled, False if
        no live session matched. Callers use the return value to
        decide between "OK, 停了" and "没在跑".
        """
        token = self.get(key)
        if token is None:
            return False
        token.cancel(reason=reason)
        logger.info(
            "cancel requested for session key=%s reason=%s",
            key.describe(),
            reason,
        )
        return True


# Module-level default registry for the common single-process case.
# Tests can instantiate their own to avoid cross-test pollution.
GLOBAL_REGISTRY = CancelTokenRegistry()
