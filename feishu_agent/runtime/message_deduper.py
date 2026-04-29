from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class MessageDeduperEntry:
    status: str
    first_seen_monotonic: float
    last_seen_monotonic: float


class MessageDeduper:
    def __init__(self, *, ttl_seconds: float = 3600.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._entries: dict[str, MessageDeduperEntry] = {}

    def should_process(self, event_key: str | None) -> bool:
        if not event_key:
            return True

        now = time.monotonic()
        with self._lock:
            self._prune(now)
            entry = self._entries.get(event_key)
            if entry is not None:
                entry.last_seen_monotonic = now
                return False
            self._entries[event_key] = MessageDeduperEntry(
                status="in_progress",
                first_seen_monotonic=now,
                last_seen_monotonic=now,
            )
            return True

    def mark_finished(self, event_key: str | None, *, keep: bool) -> None:
        if not event_key:
            return

        now = time.monotonic()
        with self._lock:
            self._prune(now)
            if not keep:
                self._entries.pop(event_key, None)
                return

            entry = self._entries.get(event_key)
            if entry is None:
                self._entries[event_key] = MessageDeduperEntry(
                    status="completed",
                    first_seen_monotonic=now,
                    last_seen_monotonic=now,
                )
                return

            entry.status = "completed"
            entry.last_seen_monotonic = now

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, entry in self._entries.items()
            if now - entry.last_seen_monotonic > self.ttl_seconds
        ]
        for key in expired:
            self._entries.pop(key, None)
