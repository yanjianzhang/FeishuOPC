"""B-1 read-compat + dual-write tests for ``SprintStateService``.

Ensures that:

1. A sprint YAML without a ``tasks:`` block still produces a
   coherent :class:`Task` list via synth from legacy flat lists.
2. Writes via ``write_tasks`` / ``update_task`` only touch the
   ``tasks:`` key — legacy lists and other top-level fields are
   preserved byte-for-byte (round-tripped through yaml.safe_dump).
3. ``advance()`` dual-writes: the legacy list gets the status
   change AND the ``tasks:`` block is upserted with a matching
   entry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from feishu_agent.team.sprint_state_service import SprintStateService
from feishu_agent.team.task_graph import Task, TaskNotFoundError


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _make(tmp_path: Path, data: dict[str, Any]) -> SprintStateService:
    _write_yaml(tmp_path / "sprint-status.yaml", data)
    return SprintStateService(tmp_path, "sprint-status.yaml")


# ---------------------------------------------------------------------------
# Read-compat: legacy flat lists synthesise into Task entries
# ---------------------------------------------------------------------------


def test_legacy_flat_lists_synthesise_tasks(tmp_path: Path) -> None:
    sprint = _make(
        tmp_path,
        {
            "sprint_name": "S",
            "current_sprint": {
                "planned": ["story-1", "story-2"],
                "in_progress": ["story-3"],
                "completed": ["story-0"],
            },
        },
    )
    tasks = sprint.load_tasks()
    by_id = {t.id: t for t in tasks}
    assert by_id["story-0"].status == "done"
    assert by_id["story-3"].status == "in-progress"
    assert by_id["story-1"].status == "pending"
    assert by_id["story-2"].status == "pending"


def test_tasks_block_takes_precedence_over_legacy(tmp_path: Path) -> None:
    """When both ``tasks:`` and the legacy flat lists exist, the
    DAG is authoritative. The legacy list stays for human-facing
    sprint tracking but doesn't double-count."""
    sprint = _make(
        tmp_path,
        {
            "current_sprint": {"in_progress": ["ghost"], "planned": []},
            "tasks": [{"id": "A", "status": "pending"}],
        },
    )
    ids = [t.id for t in sprint.load_tasks()]
    assert ids == ["A"]


# ---------------------------------------------------------------------------
# Writes don't clobber unrelated keys
# ---------------------------------------------------------------------------


def test_write_tasks_preserves_other_keys(tmp_path: Path) -> None:
    sprint = _make(
        tmp_path,
        {
            "sprint_name": "S",
            "current_sprint": {"planned": ["x"]},
            "operator_notes": "do not touch",
        },
    )
    sprint.write_tasks([Task(id="A", status="pending")])

    data = yaml.safe_load((tmp_path / "sprint-status.yaml").read_text("utf-8"))
    assert data["sprint_name"] == "S"
    assert data["current_sprint"] == {"planned": ["x"]}
    assert data["operator_notes"] == "do not touch"
    assert data["tasks"] == [
        {
            "id": "A",
            "status": "pending",
            "assignee": None,
            "blockedBy": [],
            "blocks": [],
            "concurrency_group": None,
            "priority": "normal",
            "claim": None,
            "created_at": 0,
            "updated_at": 0,
        }
    ]


def test_update_task_is_cas(tmp_path: Path) -> None:
    sprint = _make(
        tmp_path,
        {"tasks": [
            {"id": "A", "status": "pending"},
            {"id": "B", "status": "pending"},
        ]},
    )

    def _promote(t: Task) -> Task:
        t.status = "in-progress"
        return t

    updated = sprint.update_task("B", _promote)
    assert updated.status == "in-progress"
    # A is untouched; the tasks list order preserved.
    loaded = sprint.load_tasks()
    assert [t.id for t in loaded] == ["A", "B"]
    assert loaded[0].status == "pending"
    assert loaded[1].status == "in-progress"


def test_update_task_missing_id_raises(tmp_path: Path) -> None:
    sprint = _make(tmp_path, {"tasks": [{"id": "A", "status": "pending"}]})
    with pytest.raises(TaskNotFoundError):
        sprint.update_task("missing", lambda t: t)


def test_update_task_mutator_exception_does_not_persist(tmp_path: Path) -> None:
    sprint = _make(tmp_path, {"tasks": [{"id": "A", "status": "pending"}]})

    def _raise(t: Task) -> Task:
        raise ValueError("no go")

    with pytest.raises(ValueError):
        sprint.update_task("A", _raise)
    # State on disk is unchanged.
    loaded = sprint.load_tasks()
    assert loaded[0].status == "pending"


# ---------------------------------------------------------------------------
# advance() dual-writes tasks:
# ---------------------------------------------------------------------------


def test_advance_upserts_tasks_block(tmp_path: Path) -> None:
    """After ``advance()``, the ``tasks:`` block includes an entry
    for the story that just moved, matching its new status."""
    sprint = _make(
        tmp_path,
        {
            "current_sprint": {
                "planned": ["story-1"],
                "in_progress": [],
                "completed": [],
            }
        },
    )
    changes = sprint.advance(records=[], story_key="story-1")
    assert changes
    tasks = sprint.load_tasks()
    by_id = {t.id: t for t in tasks}
    assert "story-1" in by_id
    assert by_id["story-1"].status == "in-progress"
