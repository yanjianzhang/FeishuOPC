"""Per-thread ``Task`` abstraction built on top of :mod:`task_event_log`.

A ``Task`` is "one Feishu thread" elevated to a first-class object:

- ``task_id`` is stable across Feishu messages that share a ``root_id``.
- ``events.jsonl`` is the single source of truth for everything that
  happens in the thread.
- ``state.json`` is an optional snapshot that lets the runtime skip
  replaying the full log on resume.
- a process-local ``asyncio.Lock`` serializes tool-loop execution per
  ``task_id`` so a user mashing the same thread with back-to-back
  messages won't get two concurrent LLM sessions clobbering state.

The service is deliberately minimal: it opens / resumes / closes logs
and exposes the underlying :class:`TaskEventLog` to callers. Structured
``TaskState`` (mode / plan / todos / tool_health) lives in
:mod:`task_state` (M2) and will hang off this service.

Design decisions
----------------
- **Process-scoped cache**. Keeping ``TaskEventLog`` instances alive
  across messages amortizes the ``_scan_last_seq`` pass and lets the
  ``asyncio.Lock`` actually serialize same-thread concurrent messages.
- **No dual-write policy here**. The service emits events and never
  writes to the legacy ``conversations/`` / ``pending/`` /
  ``.feishu_run_history.jsonl`` files directly. Dual-write is the
  runtime's responsibility during M1 transition.
- **``ack_message_inbound`` is idempotent**. The de-duper upstream
  already rejects repeats, but the service still checks for a prior
  ``message.inbound`` event with the same ``message_id`` so a manual
  retry (e.g., re-deploying mid-run) doesn't double-log.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from feishu_agent.team.task_event_log import (
    TaskEvent,
    TaskEventLog,
    TaskKey,
)

logger = logging.getLogger(__name__)


_TASK_DIR_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


@dataclass(frozen=True)
class TaskMeta:
    """Invariants captured once at task open.

    Stored on disk under ``meta.json`` so a cold-boot replay can still
    recover the bot / chat identity that produced the log even if the
    repo config changes between runs.
    """

    task_id: str
    bot_name: str
    chat_id: str
    root_id: str
    role_name: str | None
    project_id: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "bot_name": self.bot_name,
            "chat_id": self.chat_id,
            "root_id": self.root_id,
            "role_name": self.role_name,
            "project_id": self.project_id,
            "created_at": self.created_at,
        }


class TaskHandle:
    """Convenience wrapper bundling a log, meta, and per-task asyncio lock.

    ``TaskService.open_or_resume`` returns a handle; callers append
    events via ``handle.append(...)`` or ``handle.log.append(...)``
    (same thing). The ``async with handle.session_lock()`` pattern
    serializes tool-loop execution per task_id.
    """

    def __init__(
        self,
        *,
        key: TaskKey,
        log: TaskEventLog,
        meta: TaskMeta,
        lock: asyncio.Lock,
    ) -> None:
        self._key = key
        self._log = log
        self._meta = meta
        self._lock = lock

    @property
    def task_id(self) -> str:
        return self._meta.task_id

    @property
    def key(self) -> TaskKey:
        return self._key

    @property
    def log(self) -> TaskEventLog:
        return self._log

    @property
    def meta(self) -> TaskMeta:
        return self._meta

    def append(
        self,
        *,
        kind: str,
        payload: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> TaskEvent:
        return self._log.append(
            kind=kind,
            payload=payload,
            trace_id=trace_id,
            task_id=self._meta.task_id,
        )

    def events(self, *, from_seq: int = 0) -> list[TaskEvent]:
        return self._log.read_events(from_seq=from_seq)

    @asynccontextmanager
    async def session_lock(self) -> AsyncIterator[None]:
        """Serialize execution within a single task.

        Yields once we hold the per-task asyncio lock; the caller's
        ``async with`` block is the critical section. We do NOT hold
        the ``fcntl`` flock here — ``TaskEventLog.append`` already
        uses it per write. The asyncio lock is for the async code path
        (a second message arriving while the first LLM session is
        still running on the same thread).
        """
        await self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()


class TaskService:
    """Process-local registry of :class:`TaskHandle` by ``task_id``.

    Intended lifetime is "as long as the process runs"; tasks are
    opened on demand and resumed transparently when their ``task_id``
    already has an on-disk event log.

    The module-level :data:`TASK_SERVICE` is the default instance used
    by the Feishu runtime. Tests may construct their own with a
    throwaway ``tasks_root``.
    """

    def __init__(self, tasks_root: Path) -> None:
        self._tasks_root = tasks_root
        self._tasks_root.mkdir(parents=True, exist_ok=True)
        self._handles: dict[str, TaskHandle] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._mem_lock = threading.Lock()

    @property
    def tasks_root(self) -> Path:
        return self._tasks_root

    def open_or_resume(
        self,
        key: TaskKey,
        *,
        role_name: str | None = None,
        project_id: str | None = None,
    ) -> TaskHandle:
        """Return a handle for ``key``; create the on-disk log if needed.

        The lock-and-check pattern here protects against two async
        tasks opening the same ``task_id`` simultaneously. We do NOT
        use ``asyncio.Lock`` at this layer because ``open_or_resume``
        is used from both sync and async contexts.
        """
        task_id = key.task_id()
        if not _TASK_DIR_RE.match(task_id):
            raise ValueError(f"Unsafe task_id derived from key: {task_id!r}")

        with self._mem_lock:
            handle = self._handles.get(task_id)
            if handle is not None:
                return handle

            task_dir = self._tasks_root / task_id
            log = TaskEventLog(task_dir)

            meta_dict = log.read_meta()
            is_resume = bool(meta_dict)
            if is_resume:
                meta = TaskMeta(
                    task_id=str(meta_dict.get("task_id") or task_id),
                    bot_name=str(meta_dict.get("bot_name") or key.bot_name),
                    chat_id=str(meta_dict.get("chat_id") or key.chat_id),
                    root_id=str(meta_dict.get("root_id") or key.root_id),
                    role_name=meta_dict.get("role_name"),
                    project_id=meta_dict.get("project_id"),
                    created_at=str(meta_dict.get("created_at") or ""),
                )
            else:
                from feishu_agent.team.task_event_log import _now_iso

                meta = TaskMeta(
                    task_id=task_id,
                    bot_name=key.bot_name,
                    chat_id=key.chat_id,
                    root_id=key.root_id,
                    role_name=role_name,
                    project_id=project_id,
                    created_at=_now_iso(),
                )
                log.write_meta(meta.to_dict())
                log.append(
                    kind="task.opened",
                    payload={
                        "bot_name": meta.bot_name,
                        "chat_id": meta.chat_id,
                        "root_id": meta.root_id,
                        "role_name": role_name,
                        "project_id": project_id,
                    },
                )

            lock = self._locks.setdefault(task_id, asyncio.Lock())
            handle = TaskHandle(key=key, log=log, meta=meta, lock=lock)
            self._handles[task_id] = handle

            if is_resume:
                log.append(
                    kind="task.resumed",
                    payload={
                        "role_name": role_name,
                        "project_id": project_id,
                    },
                )
            return handle

    def close(self, task_id: str, *, reason: str = "done") -> None:
        """Emit ``task.closed`` and drop the in-memory handle.

        Callers typically skip this for the ``per_thread`` lifetime
        model: a task stays open as long as the thread exists and
        just receives more events on the next Feishu message.
        Provided for tests and admin tools.
        """
        with self._mem_lock:
            handle = self._handles.pop(task_id, None)
            self._locks.pop(task_id, None)
        if handle is None:
            return
        try:
            handle.append(kind="task.closed", payload={"reason": reason})
        except Exception:  # pragma: no cover — best-effort
            logger.warning(
                "emit task.closed failed task_id=%s", task_id, exc_info=True
            )

    def iter_task_ids(self) -> Iterable[str]:
        if not self._tasks_root.exists():
            return []
        return sorted(
            p.name
            for p in self._tasks_root.iterdir()
            if p.is_dir() and _TASK_DIR_RE.match(p.name)
        )


# ---------------------------------------------------------------------------
# Module-level default instance. ``feishu_runtime_service`` (and tests)
# build this once per process and address tasks by ``TaskKey``.
# ---------------------------------------------------------------------------


_DEFAULT_SERVICE: TaskService | None = None
_DEFAULT_LOCK = threading.Lock()


def get_default_task_service(
    tasks_root: Path | None = None,
) -> TaskService:
    """Return (or create) the process-wide :class:`TaskService`.

    The first caller "wins" the root directory; subsequent calls that
    pass a different ``tasks_root`` get the existing instance. Tests
    that want a clean service should construct one directly.
    """
    global _DEFAULT_SERVICE
    with _DEFAULT_LOCK:
        if _DEFAULT_SERVICE is None:
            if tasks_root is None:
                tasks_root = Path(os.environ.get("FEISHU_TASKS_ROOT", "data/tasks"))
            _DEFAULT_SERVICE = TaskService(tasks_root)
        return _DEFAULT_SERVICE


def reset_default_task_service_for_test() -> None:
    """Test-only hook — drops the module-level singleton.

    Production never calls this; it's here so a test fixture can
    relocate ``tasks_root`` between cases without leaking state.
    """
    global _DEFAULT_SERVICE
    with _DEFAULT_LOCK:
        _DEFAULT_SERVICE = None


__all__ = [
    "TaskHandle",
    "TaskMeta",
    "TaskService",
    "get_default_task_service",
    "reset_default_task_service_for_test",
]
