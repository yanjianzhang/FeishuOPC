"""B-1 TaskGraph unit tests.

Covers the pull-mode scheduling semantics that the spec sketch in
``B-1-task-graph-dag.md`` promises: runnable-set derivation,
concurrency-group dedup, CAS happy path, double-claim conflict,
ownership errors on foreign release, and cycle detection.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from feishu_agent.team.audit_service import AuditService
from feishu_agent.team.sprint_state_service import SprintStateService
from feishu_agent.team.task_graph import (
    ClaimConflictError,
    ClaimOwnershipError,
    DagCycleError,
    Task,
    TaskGraph,
    TaskNotFoundError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed(
    tmp_path: Path,
    tasks: list[dict[str, Any]] | None = None,
) -> SprintStateService:
    path = tmp_path / "sprint-status.yaml"
    payload: dict[str, Any] = {"sprint_name": "test"}
    if tasks is not None:
        payload["tasks"] = tasks
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return SprintStateService(tmp_path, "sprint-status.yaml")


def _make_graph(
    sprint: SprintStateService,
    events: list[tuple[str, dict[str, Any]]] | None = None,
    now: int | None = None,
) -> TaskGraph:
    audit = AuditService(sprint.repo_root / "audit")

    def _emit(kind: str, payload: dict[str, Any]) -> None:
        if events is not None:
            events.append((kind, payload))

    return TaskGraph(
        sprint,
        audit,
        audit_emit=_emit,
        now_fn=(lambda: now) if now is not None else None,
    )


# ---------------------------------------------------------------------------
# Runnable derivation
# ---------------------------------------------------------------------------


def test_runnable_set_respects_blocked_by(tmp_path: Path) -> None:
    sprint = _seed(
        tmp_path,
        tasks=[
            {"id": "A", "status": "done"},
            {"id": "B", "status": "pending", "blockedBy": ["A"]},
            {"id": "C", "status": "pending", "blockedBy": ["B"]},
        ],
    )
    graph = _make_graph(sprint)
    runnable_ids = [t.id for t in graph.list_runnable()]
    # A already done, so B unblocked; C still waits on B.
    assert runnable_ids == ["B"]


def test_runnable_excludes_claimed_pending(tmp_path: Path) -> None:
    """A task in ``pending`` that somehow still has a non-expired
    claim attached is NOT runnable. This shouldn't happen via the
    public API (claim() transitions to in-progress) but is
    defense-in-depth for hand-edited YAML."""
    now = int(time.time())
    sprint = _seed(
        tmp_path,
        tasks=[
            {
                "id": "A",
                "status": "pending",
                "claim": {
                    "trace_id": "ghost",
                    "acquired_at": now - 5,
                    "expires_at": now + 300,
                },
            }
        ],
    )
    graph = _make_graph(sprint, now=now)
    assert graph.list_runnable() == []


def test_concurrency_group_dedup(tmp_path: Path) -> None:
    sprint = _seed(
        tmp_path,
        tasks=[
            {"id": "A", "status": "pending", "concurrency_group": "bitable"},
            {"id": "B", "status": "pending", "concurrency_group": "bitable"},
            {"id": "C", "status": "pending", "concurrency_group": None},
        ],
    )
    graph = _make_graph(sprint)
    ids = [t.id for t in graph.list_runnable()]
    # A wins the group; B hides; C has no group so it surfaces too.
    assert ids == ["A", "C"]


def test_priority_orders_runnable(tmp_path: Path) -> None:
    sprint = _seed(
        tmp_path,
        tasks=[
            {"id": "A", "status": "pending", "priority": "low",
             "created_at": 1},
            {"id": "B", "status": "pending", "priority": "high",
             "created_at": 2},
            {"id": "C", "status": "pending", "priority": "normal",
             "created_at": 3},
        ],
    )
    graph = _make_graph(sprint)
    ids = [t.id for t in graph.list_runnable()]
    assert ids == ["B", "C", "A"]


# ---------------------------------------------------------------------------
# Claim lifecycle — happy path
# ---------------------------------------------------------------------------


def test_claim_release_reclaim(tmp_path: Path) -> None:
    sprint = _seed(tmp_path, tasks=[{"id": "A", "status": "pending"}])
    events: list[tuple[str, dict[str, Any]]] = []
    graph = _make_graph(sprint, events=events)

    first = graph.claim("A", trace_id="tl-1", ttl_seconds=60)
    assert first.status == "in-progress"
    assert first.claim is not None
    assert first.claim.trace_id == "tl-1"

    graph.release("A", trace_id="tl-1")
    # Now A is pending again and can be claimed by someone else.
    second = graph.claim("A", trace_id="tl-2", ttl_seconds=60)
    assert second.claim is not None
    assert second.claim.trace_id == "tl-2"

    kinds = [k for k, _ in events]
    assert kinds == ["claim.acquire", "claim.release", "claim.acquire"]


def test_complete_makes_dependent_runnable(tmp_path: Path) -> None:
    sprint = _seed(
        tmp_path,
        tasks=[
            {"id": "A", "status": "pending"},
            {"id": "B", "status": "pending", "blockedBy": ["A"]},
        ],
    )
    graph = _make_graph(sprint)
    assert [t.id for t in graph.list_runnable()] == ["A"]
    graph.claim("A", "tl", 60)
    graph.complete("A", "tl")
    assert [t.id for t in graph.list_runnable()] == ["B"]


# ---------------------------------------------------------------------------
# Claim lifecycle — error paths
# ---------------------------------------------------------------------------


def test_double_claim_raises(tmp_path: Path) -> None:
    sprint = _seed(tmp_path, tasks=[{"id": "A", "status": "pending"}])
    graph = _make_graph(sprint)
    graph.claim("A", trace_id="tl-1", ttl_seconds=60)
    with pytest.raises(ClaimConflictError):
        graph.claim("A", trace_id="tl-2", ttl_seconds=60)


def test_claim_on_done_raises(tmp_path: Path) -> None:
    sprint = _seed(tmp_path, tasks=[{"id": "A", "status": "done"}])
    graph = _make_graph(sprint)
    with pytest.raises(ClaimConflictError):
        graph.claim("A", trace_id="tl", ttl_seconds=60)


def test_foreign_release_raises(tmp_path: Path) -> None:
    sprint = _seed(tmp_path, tasks=[{"id": "A", "status": "pending"}])
    graph = _make_graph(sprint)
    graph.claim("A", trace_id="tl-1", ttl_seconds=60)
    with pytest.raises(ClaimOwnershipError):
        graph.release("A", trace_id="not-the-owner")


def test_unknown_task_raises(tmp_path: Path) -> None:
    sprint = _seed(tmp_path, tasks=[{"id": "A", "status": "pending"}])
    graph = _make_graph(sprint)
    with pytest.raises(TaskNotFoundError):
        graph.claim("Z", trace_id="tl", ttl_seconds=60)


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


def test_cycle_detection(tmp_path: Path) -> None:
    sprint = _seed(
        tmp_path,
        tasks=[
            {"id": "A", "status": "pending", "blockedBy": ["B"]},
            {"id": "B", "status": "pending", "blockedBy": ["A"]},
        ],
    )
    graph = _make_graph(sprint)
    with pytest.raises(DagCycleError):
        graph.validate_no_cycles()


def test_cycle_detection_warns_on_unknown_deps(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """M3 fix — a dep pointing to a non-existent task id is not a
    cycle (no raise), but it IS a data-integrity red flag because
    ``is_runnable`` treats unknown deps as "never done" and the task
    silently becomes unschedulable. We now emit a WARNING so the
    operator notices without grepping the YAML."""
    import logging as _logging

    sprint = _seed(
        tmp_path,
        tasks=[{"id": "A", "status": "pending", "blockedBy": ["Z"]}],
    )
    graph = _make_graph(sprint)
    with caplog.at_level(_logging.WARNING, logger="feishu_agent.team.task_graph"):
        graph.validate_no_cycles()  # no raise
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "unknown tasks" in m and "1" in m for m in messages
    ), f"expected unknown-dep warning, got: {messages}"


def test_acyclic_validates(tmp_path: Path) -> None:
    sprint = _seed(
        tmp_path,
        tasks=[
            {"id": "A", "status": "done"},
            {"id": "B", "status": "pending", "blockedBy": ["A"]},
            {"id": "C", "status": "pending", "blockedBy": ["B"]},
        ],
    )
    graph = _make_graph(sprint)
    graph.validate_no_cycles()


# ---------------------------------------------------------------------------
# Task dataclass roundtrip
# ---------------------------------------------------------------------------


def test_task_dict_roundtrip() -> None:
    t = Task(
        id="X",
        status="in-progress",
        assignee="someone",
        blocked_by=["A", "B"],
        blocks=["C"],
        concurrency_group="git",
        priority="high",
        created_at=10,
        updated_at=20,
    )
    rehydrated = Task.from_dict(t.to_dict())
    assert rehydrated == t


def test_task_from_dict_tolerates_bad_status() -> None:
    """Unknown status values degrade to ``pending`` so an old YAML
    with a typo doesn't crash the loader."""
    t = Task.from_dict({"id": "X", "status": "in-limbo"})
    assert t.status == "pending"
