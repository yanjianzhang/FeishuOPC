"""Append-only event log for a ``Task`` (one per Feishu thread).

Why this module exists
----------------------
Until this module, a ``bot session`` was ephemeral: one Feishu message
spun up ``LlmAgentAdapter``, ran a tool loop, and the only traces left
behind were scattered derivatives (``conversations/<role>.jsonl``,
``pending/<trace>.json``, ``.feishu_run_history.jsonl`` digest). None
of them captured the full multi-message thread as an ordered stream —
so the bot could not be replayed, audited end-to-end, or resumed after
a crash.

The ``TaskEventLog`` flips that: every meaningful lifecycle event
(``llm.request``, ``tool.call``, ``tool.result``, ``state.mode_set``,
``reminder.emitted`` …) is appended as one NDJSON line keyed by a
stable ``task_id`` that we derive from ``(bot_name, chat_id, root_id
or message_id)``. The file is the single source of truth; every other
artifact in ``data/techbot-runs/`` becomes a downstream projection.

Design decisions
----------------
- **NDJSON, append-only**. One event per line, never rewritten.
  ``tail -f`` and ``grep`` still work; no file format to version.
- **``fcntl`` lock on append**. Same flock pattern as
  ``LastRunMemoryService``; guarantees that concurrent writers on the
  same thread don't interleave bytes mid-line. Rotation (see
  ``snapshot_and_rotate``) is guarded by the same lock.
- **Monotonic ``seq``**. Each event carries a strictly-increasing
  integer so ``state.json`` can record a ``base_seq`` checkpoint and
  consumers only replay events above it.
- **POSIX-only locking**. Windows gets a best-effort no-op — matches
  every other lock in this codebase.
- **Not a queue**. No subscriber/fanout machinery. Downstream
  projections ( ``RunDigestCollector``, ``ReminderBus``, …) subscribe
  to the same ``HookBus`` they already use; the log is only for
  persistence.

Non-goals
---------
- **No distributed coordination**. Single-process assumption matches
  ``CancelTokenRegistry``. A cross-host deployment would need redis
  or similar.
- **No schema evolution machinery**. Event ``kind`` is a string; if
  we rename one later, downstream consumers need to cope — the JSONL
  can still be replayed verbatim.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

try:  # POSIX only; Windows gets a best-effort no-op lock.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover
    _fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# Canonical event kinds. Listed as a single source of truth for code
# review; subscribers are NOT required to use the enum but writers
# should stick to the list so we don't accumulate typos.
#
# Schema note (spec 004 / A-3): new event families are ADDITIVE —
# ``artifact.*``, ``claim.*``, ``worktree.*``, ``fanout.*`` were
# introduced as part of 004 and older projectors silently ignore
# them via the "unknown kind → counted but not interpreted" rule in
# ``task_replay.replay``. No schema_version bump was needed on the
# log format itself; only the ``KNOWN_EVENT_KINDS`` set grew.
KNOWN_EVENT_KINDS: frozenset[str] = frozenset(
    {
        # Task lifecycle.
        "task.opened",
        "task.closed",
        "task.resumed",
        # Feishu IO.
        "message.inbound",
        "message.outbound",
        # LLM loop.
        "llm.request",
        "llm.response",
        "llm.compression",
        # Tool calls.
        "tool.call",
        "tool.result",
        "tool.error",
        # Structured self-state mutations (set by TaskStateExecutor).
        "state.mode_set",
        "state.plan_set",
        "state.plan_step_updated",
        "state.todo_added",
        "state.todo_updated",
        "state.todo_done",
        "state.note_added",
        # Reminders injected back into the LLM view.
        "reminder.emitted",
        # External world outcomes (git / CI / PR).
        "world.git",
        "world.ci",
        "world.pr",
        "world.pre_push",
        # Confirmation / pending action pipeline.
        "pending.requested",
        "pending.resolved",
        # Memory maintenance projections.
        "memory.candidates_generated",
        # Spec 004 / A-3: role artifact envelope persistence.
        # Payload: {artifact_id, role, success, stop_reason,
        # risk_score, duration_ms, artifact_path, error?}.
        "artifact.write",
        # Spec 004 / B-1: DAG task-graph claim lease telemetry.
        # Payload for acquire: {task_id, trace_id, expires_at}.
        # Payload for release: {task_id, trace_id, reason}. The
        # ``.expired`` variant is fired specifically during
        # ``release_expired()`` so analytics can separate "ran to
        # completion" from "lease timed out".
        "claim.acquire",
        "claim.release",
        "claim.expired",
        # Spec 004 / B-3: git worktree provisioning lifecycle.
        # Payload for acquire: {child_trace_id, path, branch,
        # fallback}; for release: {child_trace_id, success}.
        "worktree.acquire",
        "worktree.release",
        # Spec 004 / B-2: effect-aware tool-call fan-out.
        # Payload for begin: {turn, groups: [{size, mode}, ...]}.
        # Payload for end: {turn, total_duration_ms}.
        "fanout.begin",
        "fanout.end",
    }
)


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class TaskKey:
    """Stable identity for a per-thread task.

    Derived from the Feishu bot / chat / (optional) thread root. When
    the message is not part of a topic, ``root_id`` falls back to the
    message id itself — the task degenerates to one message, but the
    code path stays uniform.
    """

    bot_name: str
    chat_id: str
    root_id: str  # root topic id OR falls back to message_id

    @classmethod
    def derive(
        cls,
        *,
        bot_name: str,
        chat_id: str | None,
        root_id: str | None,
        message_id: str | None,
    ) -> "TaskKey":
        cid = (chat_id or "").strip() or "nochat"
        rid = (root_id or "").strip() or (message_id or "").strip() or "noroot"
        bn = (bot_name or "").strip() or "default"
        return cls(bot_name=bn, chat_id=cid, root_id=rid)

    def short_hash(self) -> str:
        raw = f"{self.bot_name}|{self.chat_id}|{self.root_id}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:12]

    def task_id(self) -> str:
        """Short-but-greppable id used for filenames.

        Layout: ``<bot>-<12hex>``. ``bot_name`` is always ASCII and
        already used in ``CancelKey.describe``; keeping it in the dir
        name makes ``ls data/tasks/`` instantly useful.
        """
        bn = re.sub(r"[^A-Za-z0-9_]+", "_", self.bot_name)[:32] or "bot"
        return f"{bn}-{self.short_hash()}"


@dataclass
class TaskEvent:
    """One line in ``events.jsonl``.

    ``payload`` is intentionally a plain dict — we want the event log
    to be readable / patched by hand if something goes sideways, which
    rules out tagged unions and nested dataclasses.
    """

    task_id: str
    seq: int
    kind: str
    ts: str = field(default_factory=_now_iso)
    trace_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        obj = asdict(self)
        # None payload keys convey information ("trace_id unknown"),
        # don't dropped them defensively.
        return json.dumps(obj, ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, line: str) -> "TaskEvent | None":
        line = line.strip()
        if not line:
            return None
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(raw, dict):
            return None
        try:
            return cls(
                task_id=str(raw.get("task_id") or ""),
                seq=int(raw.get("seq") or 0),
                kind=str(raw.get("kind") or ""),
                ts=str(raw.get("ts") or ""),
                trace_id=raw.get("trace_id"),
                payload=raw.get("payload") or {},
            )
        except (TypeError, ValueError):
            return None


class TaskEventLog:
    """Append-only NDJSON log for one task.

    Thread-safe per-process via ``threading.Lock`` and cross-process
    safe via POSIX ``fcntl.flock``. Every public write path goes
    through ``append()`` so the two locks compose correctly.

    Directory layout on disk::

        <root>/
          events.jsonl      # append-only NDJSON, source of truth
          state.json        # optional snapshot (written by TaskService)
          meta.json         # invariants (task_id / bot / chat / root_id)
          lock              # flock target (never contains payload)

    ``events.jsonl`` is the authoritative ledger; ``state.json`` is a
    cache that accelerates resume. Deleting the snapshot is always
    safe — callers just replay from ``seq=0``.
    """

    EVENTS_FILENAME = "events.jsonl"
    STATE_FILENAME = "state.json"
    META_FILENAME = "meta.json"
    LOCK_FILENAME = "lock"

    def __init__(self, task_dir: Path) -> None:
        if not _SAFE_ID_RE.match(task_dir.name):
            raise ValueError(
                f"TaskEventLog dir name must match {_SAFE_ID_RE.pattern!r}; "
                f"got {task_dir.name!r}"
            )
        self._dir = task_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._events_path = task_dir / self.EVENTS_FILENAME
        self._state_path = task_dir / self.STATE_FILENAME
        self._meta_path = task_dir / self.META_FILENAME
        self._lock_path = task_dir / self.LOCK_FILENAME
        self._mem_lock = threading.Lock()
        self._next_seq = self._scan_last_seq() + 1

    @property
    def dir(self) -> Path:
        return self._dir

    @property
    def events_path(self) -> Path:
        return self._events_path

    @property
    def state_path(self) -> Path:
        return self._state_path

    @property
    def meta_path(self) -> Path:
        return self._meta_path

    @property
    def next_seq(self) -> int:
        return self._next_seq

    # --- meta / snapshot --------------------------------------------------

    def read_meta(self) -> dict[str, Any]:
        if not self._meta_path.exists():
            return {}
        try:
            return json.loads(self._meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def write_meta(self, meta: dict[str, Any]) -> None:
        with self._locked_for_write():
            tmp = self._meta_path.with_suffix(self._meta_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._meta_path)

    def read_snapshot(self) -> dict[str, Any] | None:
        if not self._state_path.exists():
            return None
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def write_snapshot(self, snapshot: dict[str, Any]) -> None:
        with self._locked_for_write():
            tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._state_path)

    # --- append / iterate -------------------------------------------------

    def append(
        self,
        *,
        kind: str,
        payload: dict[str, Any] | None = None,
        trace_id: str | None = None,
        task_id: str | None = None,
    ) -> TaskEvent:
        """Append one event; returns the stamped event (with ``seq``)."""
        if not kind:
            raise ValueError("TaskEvent.kind must be non-empty")
        if kind not in KNOWN_EVENT_KINDS:
            # Unknown kinds are not forbidden — extension without a
            # central registry is a design goal — but we log once so
            # typos during development stand out.
            logger.debug("task event using unknown kind=%r", kind)
        with self._locked_for_write(), self._mem_lock:
            event = TaskEvent(
                task_id=task_id or self._dir.name,
                seq=self._next_seq,
                kind=kind,
                trace_id=trace_id,
                payload=dict(payload or {}),
            )
            self._next_seq += 1
            line = event.to_json()
            # Belt-and-suspenders: never allow an embedded newline to
            # split the record across two lines.
            if "\n" in line:
                line = line.replace("\n", " ")
            with self._events_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
            return event

    def iter_events(self, *, from_seq: int = 0) -> Iterator[TaskEvent]:
        if not self._events_path.exists():
            return iter(())
        def _gen() -> Iterator[TaskEvent]:
            with self._events_path.open("r", encoding="utf-8") as handle:
                for raw in handle:
                    event = TaskEvent.from_json(raw)
                    if event is None:
                        continue
                    if event.seq < from_seq:
                        continue
                    yield event
        return _gen()

    def read_events(self, *, from_seq: int = 0) -> list[TaskEvent]:
        return list(self.iter_events(from_seq=from_seq))

    # --- low level --------------------------------------------------------

    def _scan_last_seq(self) -> int:
        """Return the highest ``seq`` already written (or ``-1`` when empty).

        Called once at construction. O(n) in the number of events — we
        don't maintain an index; for the realistic ``O(hundreds)`` per
        task this is trivially fast. If a task ever grows enough for
        this to matter, ``snapshot_and_rotate`` will clip the file
        before scanning becomes expensive.
        """
        if not self._events_path.exists():
            return -1
        last = -1
        try:
            with self._events_path.open("r", encoding="utf-8") as handle:
                for raw in handle:
                    event = TaskEvent.from_json(raw)
                    if event is None:
                        continue
                    if event.seq > last:
                        last = event.seq
        except OSError:
            logger.warning(
                "failed scanning events for %s; starting at seq=0",
                self._events_path,
                exc_info=True,
            )
            return -1
        return last

    def _locked_for_write(self):
        """flock context manager; no-op when ``fcntl`` is missing."""
        from contextlib import contextmanager

        @contextmanager
        def _cm() -> Iterable[None]:
            if _fcntl is None:  # pragma: no cover — Windows
                yield
                return
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self._lock_path), os.O_RDWR | os.O_CREAT, 0o644)
            try:
                _fcntl.flock(fd, _fcntl.LOCK_EX)
                yield
            finally:
                try:
                    _fcntl.flock(fd, _fcntl.LOCK_UN)
                finally:
                    os.close(fd)

        return _cm()


__all__ = [
    "KNOWN_EVENT_KINDS",
    "TaskEvent",
    "TaskEventLog",
    "TaskKey",
]
