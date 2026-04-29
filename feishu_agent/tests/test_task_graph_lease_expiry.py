"""B-1 / SC-004-6 lease-expiry regression tests.

A stale claim (one whose ``expires_at`` is in the past) must NOT
permanently hide a task from the runnable set. ``release_expired``
is implicitly called at the top of every read API, so the task
returns to ``pending`` on the next ``list_runnable()`` call.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import yaml

from feishu_agent.team.audit_service import AuditService
from feishu_agent.team.sprint_state_service import SprintStateService
from feishu_agent.team.task_graph import TaskGraph


def _seed_expired(tmp_path: Path) -> SprintStateService:
    now = int(time.time())
    data: dict[str, Any] = {
        "tasks": [
            {
                "id": "A",
                "status": "in-progress",
                "claim": {
                    "trace_id": "dead-agent",
                    "acquired_at": now - 3600,
                    "expires_at": now - 60,
                },
            },
            {"id": "B", "status": "pending"},
        ],
    }
    (tmp_path / "sprint-status.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )
    return SprintStateService(tmp_path, "sprint-status.yaml")


def test_list_runnable_releases_expired_lease(tmp_path: Path) -> None:
    sprint = _seed_expired(tmp_path)
    events: list[tuple[str, dict[str, Any]]] = []

    def _emit(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))

    graph = TaskGraph(
        sprint, AuditService(tmp_path / "audit"), audit_emit=_emit
    )
    runnable_ids = [t.id for t in graph.list_runnable()]
    # A's claim expired, so A is back in the runnable pool.
    assert set(runnable_ids) == {"A", "B"}
    # claim.expired was emitted exactly once for A.
    expired_events = [p for k, p in events if k == "claim.expired"]
    assert expired_events == [{"task_id": "A"}]


def test_claim_after_expiry_succeeds(tmp_path: Path) -> None:
    sprint = _seed_expired(tmp_path)
    graph = TaskGraph(sprint, AuditService(tmp_path / "audit"))
    fresh = graph.claim("A", trace_id="new-agent", ttl_seconds=60)
    assert fresh.claim is not None
    assert fresh.claim.trace_id == "new-agent"


def test_release_expired_is_idempotent_when_nothing_expired(
    tmp_path: Path,
) -> None:
    """A call with no expired leases should be a cheap no-op —
    no audit event, no write."""
    now = int(time.time())
    data = {
        "tasks": [
            {
                "id": "A",
                "status": "in-progress",
                "claim": {
                    "trace_id": "live",
                    "acquired_at": now,
                    "expires_at": now + 300,
                },
            }
        ]
    }
    (tmp_path / "sprint-status.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )
    sprint = SprintStateService(tmp_path, "sprint-status.yaml")
    events: list[tuple[str, dict[str, Any]]] = []
    graph = TaskGraph(
        sprint,
        AuditService(tmp_path / "audit"),
        audit_emit=lambda k, p: events.append((k, p)),
    )
    assert graph.release_expired() == []
    assert events == []
