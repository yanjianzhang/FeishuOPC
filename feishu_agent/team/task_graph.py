"""B-1 DAG task graph with claim lease.

Extends :mod:`feishu_agent.team.sprint_state_service` with a
pull-mode scheduling primitive. The shared task list lives in the
sprint YAML under the new ``tasks:`` key; this module layers the
following semantics on top:

* **Runnable set** — "pending tasks whose dependencies are all
  done" (DAG unblock rule).
* **Concurrency groups** — at most one runnable per group, so a
  group with 5 pending tasks shows up as 1 runnable until the
  current holder finishes. Lets roles express "don't run me in
  parallel with myself" without the scheduler needing special
  knowledge.
* **Claim lease** — a pending task can be atomically transitioned
  to ``in-progress`` with a ``(trace_id, expires_at)`` stamp.
  Expired claims are automatically released on the next read so a
  dead agent can't monopolise a task forever.
* **Cycle detection** — a best-effort walk that refuses to
  schedule if someone hand-edits the YAML into an inconsistent
  state.

Non-goals in this wave:

* No distributed coordination. One host, one process tree (the
  ``fcntl.flock`` on ``.task-graph.lock`` handles cross-process
  safety within that host).
* No priority scheduling beyond a coarse ``low/normal/high``
  label; the tie-breaker is insertion order. FIFO within priority
  matches the team-scale we target (≤ 5 concurrent roles).
* No heartbeat refresh on an existing claim. A role that runs
  longer than ``ttl_seconds`` will lose its lease. Spec
  OQ-B1-renewal explicitly defers this — it's additive and
  non-blocking for B-2.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from feishu_agent.team.audit_service import AuditService
from feishu_agent.team.sprint_state_service import SprintStateService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ClaimConflictError(RuntimeError):
    """Task already claimed or not in a claimable state."""

    def __init__(self, task_id: str, current_status: str) -> None:
        super().__init__(
            f"Task {task_id!r} cannot be claimed (current status={current_status!r})"
        )
        self.task_id = task_id
        self.current_status = current_status


class ClaimOwnershipError(RuntimeError):
    """Someone other than the claim holder tried to release/complete it."""

    def __init__(self, task_id: str, trace_id: str) -> None:
        super().__init__(
            f"Task {task_id!r} is not claimed by trace_id={trace_id!r}"
        )
        self.task_id = task_id
        self.trace_id = trace_id


class TaskNotFoundError(KeyError):
    """Requested task_id isn't in the graph."""


class DagCycleError(ValueError):
    """blockedBy graph contains a cycle — schedule is unsafe."""

    def __init__(self, node: str, stack: list[str]) -> None:
        super().__init__(
            f"Cycle in task-graph at {node!r} (stack={stack!r})"
        )
        self.node = node
        self.stack = stack


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


TaskStatus = Literal["pending", "in-progress", "done", "blocked"]
TaskPriority = Literal["low", "normal", "high"]


@dataclass
class ClaimLease:
    """A time-bounded token saying "this task belongs to trace_id".

    ``acquired_at`` / ``expires_at`` are unix-seconds ints so the
    YAML stays human-greppable. We deliberately skip wall-clock
    nanoseconds — the claim is advisory, not transactional, and
    second-level precision is plenty for the sub-minute leases
    we issue in practice.
    """

    trace_id: str
    acquired_at: int
    expires_at: int

    @classmethod
    def acquire(cls, trace_id: str, ttl_seconds: int) -> "ClaimLease":
        now = int(time.time())
        return cls(
            trace_id=trace_id,
            acquired_at=now,
            expires_at=now + max(int(ttl_seconds), 1),
        )

    def is_expired(self, now: int) -> bool:
        """``now >= expires_at`` — boundary is CLOSED on the
        expired side so a lease at exactly its TTL counts as
        already gone. Deterministic test behaviour matters more
        than a second of slack here."""
        return now >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ClaimLease | None":
        if not data:
            return None
        try:
            return cls(
                trace_id=str(data.get("trace_id") or ""),
                acquired_at=int(data.get("acquired_at") or 0),
                expires_at=int(data.get("expires_at") or 0),
            )
        except (TypeError, ValueError):
            return None


@dataclass
class Task:
    id: str
    status: TaskStatus = "pending"
    assignee: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    concurrency_group: str | None = None
    priority: TaskPriority = "normal"
    claim: ClaimLease | None = None
    created_at: int = 0
    updated_at: int = 0

    def is_runnable(self, completed_ids: set[str]) -> bool:
        """A task is runnable when it's pending, unclaimed, and
        every dep is done. Doesn't consider concurrency-group
        dedup — that's applied at the :class:`TaskGraph` level."""
        if self.status != "pending":
            return False
        if self.claim is not None:
            return False
        return all(dep in completed_ids for dep in self.blocked_by)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "assignee": self.assignee,
            "blockedBy": list(self.blocked_by),
            "blocks": list(self.blocks),
            "concurrency_group": self.concurrency_group,
            "priority": self.priority,
            "claim": self.claim.to_dict() if self.claim else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        status_val = str(data.get("status") or "pending")
        if status_val not in {"pending", "in-progress", "done", "blocked"}:
            status_val = "pending"
        priority_val = str(data.get("priority") or "normal")
        if priority_val not in {"low", "normal", "high"}:
            priority_val = "normal"
        return cls(
            id=str(data.get("id") or ""),
            status=status_val,  # type: ignore[arg-type]
            assignee=_nullable_str(data.get("assignee")),
            blocked_by=_coerce_str_list(data.get("blockedBy")),
            blocks=_coerce_str_list(data.get("blocks")),
            concurrency_group=_nullable_str(data.get("concurrency_group")),
            priority=priority_val,  # type: ignore[arg-type]
            claim=ClaimLease.from_dict(data.get("claim") or None),
            created_at=int(data.get("created_at") or 0),
            updated_at=int(data.get("updated_at") or 0),
        )


def _nullable_str(value: Any) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _coerce_str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v]
    return []


# ---------------------------------------------------------------------------
# Audit emission hook
# ---------------------------------------------------------------------------


AuditEmitter = Callable[[str, dict[str, Any]], None]


def _noop_emit(kind: str, payload: dict[str, Any]) -> None:
    """Default audit hook — logs at DEBUG so tests and bench runs
    produce no stderr noise, while a misconfigured production
    setup still leaves a trail in ``feishu-agent.log``."""
    logger.debug("task_graph emit %s %s", kind, payload)


# ---------------------------------------------------------------------------
# TaskGraph
# ---------------------------------------------------------------------------


_PRIORITY_KEY = {"high": 0, "normal": 1, "low": 2}


def _priority_key(task: Task) -> tuple[int, int]:
    # Sort high before normal before low; within same priority use
    # insertion-preserving ``created_at`` as the secondary key so a
    # deterministic scan order matches the YAML order operators see.
    return (_PRIORITY_KEY.get(task.priority, 1), task.created_at)


class TaskGraph:
    """Pull-mode scheduling view over the sprint task list.

    Every read API implicitly calls :meth:`release_expired` first
    so a stale claim held by a dead agent never permanently hides
    a task from the runnable set. The implicit release is
    idempotent when there's nothing to expire, so the overhead is
    one list scan per read.
    """

    def __init__(
        self,
        sprint_service: SprintStateService,
        audit: AuditService | None = None,
        *,
        audit_emit: AuditEmitter | None = None,
        now_fn: Callable[[], int] | None = None,
    ) -> None:
        self._sprint = sprint_service
        # ``audit`` is kept around for future richer integrations
        # (per-trace JSON dumps); current lifecycle events flow
        # through ``_emit`` so tests can observe them without
        # scraping the audit dir.
        self._audit = audit
        self._emit: AuditEmitter = audit_emit or _noop_emit
        self._now = now_fn or (lambda: int(time.time()))

    # ------------------------------------------------------------------
    # Read APIs
    # ------------------------------------------------------------------

    def list_all(self) -> list[Task]:
        self.release_expired()
        return self._sprint.load_tasks()

    def list_runnable(self) -> list[Task]:
        """Tasks whose dependencies have all completed, after
        concurrency-group dedup and priority sort.

        Sort order: high > normal > low, then insertion-order
        (``created_at`` asc). The first task in each concurrency
        group "wins" — subsequent tasks in the same group stay
        queued as pending but don't surface in the runnable list."""
        self.release_expired()
        tasks = self._sprint.load_tasks()
        completed = {t.id for t in tasks if t.status == "done"}
        runnable = [t for t in tasks if t.is_runnable(completed)]
        runnable.sort(key=_priority_key)

        seen_groups: set[str] = set()
        filtered: list[Task] = []
        for t in runnable:
            group = t.concurrency_group
            if group and group in seen_groups:
                continue
            if group:
                seen_groups.add(group)
            filtered.append(t)
        return filtered

    def get(self, task_id: str) -> Task:
        for t in self._sprint.load_tasks():
            if t.id == task_id:
                return t
        raise TaskNotFoundError(task_id)

    # ------------------------------------------------------------------
    # Write APIs
    # ------------------------------------------------------------------

    def claim(
        self,
        task_id: str,
        trace_id: str,
        ttl_seconds: int = 180,
    ) -> Task:
        """Atomically move ``task_id`` from pending → in-progress
        with a claim. Raises :class:`ClaimConflictError` if the
        task is already claimed or isn't pending."""
        self.release_expired()

        def _mutate(task: Task) -> Task:
            if task.status != "pending":
                raise ClaimConflictError(task.id, task.status)
            if task.claim is not None:
                # Defensive: release_expired above should have
                # cleared this, but if wall-clock drifted we'd
                # rather fail loud than silently double-claim.
                raise ClaimConflictError(task.id, "already-claimed")
            task.status = "in-progress"
            task.claim = ClaimLease.acquire(trace_id, ttl_seconds)
            task.assignee = trace_id
            task.updated_at = self._now()
            return task

        task = self._sprint.update_task(task_id, _mutate)
        assert task.claim is not None  # proven by _mutate
        self._emit(
            "claim.acquire",
            {
                "task_id": task_id,
                "trace_id": trace_id,
                "expires_at": task.claim.expires_at,
            },
        )
        return task

    def release(self, task_id: str, trace_id: str) -> None:
        """Voluntarily give the task back to the pending pool.

        Use this when a role aborts without completing — e.g. a
        health check failed or the user cancelled. Raises
        :class:`ClaimOwnershipError` if ``trace_id`` doesn't hold
        the lease. Failure modes:
        * task never claimed → ownership error (empty trace_id can't own anything)
        * claimed by someone else → ownership error
        * claimed by us → reset to pending
        """
        def _mutate(task: Task) -> Task:
            if task.claim is None or task.claim.trace_id != trace_id:
                raise ClaimOwnershipError(task.id, trace_id)
            task.status = "pending"
            task.claim = None
            task.assignee = None
            task.updated_at = self._now()
            return task

        self._sprint.update_task(task_id, _mutate)
        self._emit(
            "claim.release",
            {"task_id": task_id, "trace_id": trace_id, "reason": "normal"},
        )

    def complete(self, task_id: str, trace_id: str) -> None:
        """Close out a task permanently. Same ownership check as
        :meth:`release`; on success, ``status`` flips to ``done``
        and the claim is cleared so dependent tasks become
        runnable on the next :meth:`list_runnable` call."""
        def _mutate(task: Task) -> Task:
            if task.claim is None or task.claim.trace_id != trace_id:
                raise ClaimOwnershipError(task.id, trace_id)
            task.status = "done"
            task.claim = None
            task.updated_at = self._now()
            return task

        self._sprint.update_task(task_id, _mutate)
        self._emit(
            "claim.release",
            {"task_id": task_id, "trace_id": trace_id, "reason": "complete"},
        )

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def release_expired(self) -> list[str]:
        """Drop every expired claim back into the pending pool.

        Returns the list of task ids that were reset so operators
        can log it. Idempotent — calls when nothing is expired
        are a free fast path (one load + one filter).
        """
        tasks = self._sprint.load_tasks()
        now = self._now()
        expired_ids = [
            t.id for t in tasks
            if t.claim is not None and t.claim.is_expired(now)
        ]
        if not expired_ids:
            return []

        def _mutate_all(tasks: list[Task]) -> list[Task]:
            for t in tasks:
                if t.id in expired_ids:
                    t.status = "pending"
                    t.claim = None
                    t.assignee = None
                    t.updated_at = now
            return tasks

        self._sprint.update_tasks_batch(_mutate_all)
        for tid in expired_ids:
            self._emit("claim.expired", {"task_id": tid})
        return expired_ids

    def validate_no_cycles(self) -> None:
        """DFS-based cycle detection over ``blockedBy``.

        ``blockedBy`` should be a subset of ``id`` in a well-formed
        graph. Unknown ids in ``blockedBy`` are not a cycle, but
        they ARE a data-integrity red flag — :meth:`is_runnable`
        treats an unknown id as "prerequisite never done" so the
        task can silently become unschedulable. We log a single
        WARNING per unknown edge so operators notice without having
        to grep the YAML. Tests still pass because the method
        still returns ``None`` (no raise) when the graph is
        acyclic.
        """
        tasks = self._sprint.load_tasks()
        graph = {t.id: list(t.blocked_by) for t in tasks}
        unknown: set[tuple[str, str]] = set()
        for node, deps in graph.items():
            for dep in deps:
                if dep not in graph:
                    unknown.add((node, dep))
        if unknown:
            logger.warning(
                "task-graph: %d blockedBy edges reference unknown tasks; "
                "these tasks will never unblock (sample=%s)",
                len(unknown),
                sorted(unknown)[:5],
            )

        visited: set[str] = set()
        stack: list[str] = []
        stack_set: set[str] = set()

        def visit(node: str) -> None:
            if node in stack_set:
                raise DagCycleError(node, list(stack))
            if node in visited:
                return
            stack.append(node)
            stack_set.add(node)
            for dep in graph.get(node, []):
                if dep in graph:
                    visit(dep)
            stack.pop()
            stack_set.discard(node)
            visited.add(node)

        for node in graph:
            visit(node)


__all__ = [
    "ClaimConflictError",
    "ClaimLease",
    "ClaimOwnershipError",
    "DagCycleError",
    "Task",
    "TaskGraph",
    "TaskNotFoundError",
]
