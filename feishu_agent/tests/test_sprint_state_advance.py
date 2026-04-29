from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from feishu_agent.team.sprint_state_service import SprintStateError, SprintStateService

SAMPLE_STATUS = {
    "sprint_name": "phase-1",
    "current_sprint": {
        "goal": "Shared foundations",
        "in_progress": ["1-1-shared-foundations"],
        "planned": ["1-2-tool-dispatch"],
        "review": [],
        "completed": ["0-1-research-spike"],
    },
}


@dataclass
class FakeRecord:
    story_key: str
    status: str
    native_key: str = ""


def _write_status(tmp_path: Path, data: dict) -> SprintStateService:
    status_file = "sprint-status.yaml"
    (tmp_path / status_file).write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return SprintStateService(tmp_path, status_file)


def test_advance_with_explicit_story_and_status(tmp_path: Path):
    svc = _write_status(tmp_path, SAMPLE_STATUS)
    records = [FakeRecord(story_key="1-1-shared-foundations", status="in-progress")]

    changes = svc.advance(
        records,
        story_key="1-1-shared-foundations",
        to_status="review",
        reason="manual advance",
    )

    assert len(changes) == 1
    assert changes[0].story_key == "1-1-shared-foundations"
    assert changes[0].from_status == "in-progress"
    assert changes[0].to_status == "review"
    reloaded = svc.load_status_data()
    assert "1-1-shared-foundations" in reloaded["current_sprint"]["review"]
    assert "1-1-shared-foundations" not in reloaded["current_sprint"]["in_progress"]


def test_advance_auto_pick_story(tmp_path: Path):
    data = {
        "current_sprint": {
            "review": ["story-a"],
            "in_progress": ["story-b"],
            "planned": [],
            "completed": [],
        },
    }
    svc = _write_status(tmp_path, data)
    records = [
        FakeRecord(story_key="story-b", status="in-progress"),
        FakeRecord(story_key="story-a", status="review"),
    ]

    changes = svc.advance(records, reason="auto")

    assert changes[0].story_key == "story-a"
    assert changes[0].to_status == "done"


def test_advance_dry_run_does_not_write(tmp_path: Path):
    svc = _write_status(tmp_path, SAMPLE_STATUS)
    records = [FakeRecord(story_key="1-1-shared-foundations", status="in-progress")]

    changes = svc.advance(
        records,
        story_key="1-1-shared-foundations",
        to_status="review",
        dry_run=True,
    )

    assert len(changes) == 1
    reloaded = svc.load_status_data()
    assert "1-1-shared-foundations" in reloaded["current_sprint"]["in_progress"]
    assert "1-1-shared-foundations" not in reloaded["current_sprint"].get("review", [])


def test_advance_illegal_transition_raises(tmp_path: Path):
    svc = _write_status(tmp_path, SAMPLE_STATUS)
    records = [FakeRecord(story_key="1-1-shared-foundations", status="in-progress")]

    with pytest.raises(SprintStateError, match="Illegal transition"):
        svc.advance(
            records,
            story_key="1-1-shared-foundations",
            to_status="planned",
        )


def test_advance_missing_status_file_raises(tmp_path: Path):
    svc = SprintStateService(tmp_path, "nonexistent.yaml")
    records = [FakeRecord(story_key="x", status="in-progress")]

    with pytest.raises(SprintStateError, match="Sprint status file missing"):
        svc.advance(records, story_key="x")


def test_advance_no_advanceable_story_raises(tmp_path: Path):
    data = {
        "current_sprint": {
            "completed": ["only-done"],
            "in_progress": [],
            "planned": [],
            "review": [],
        },
    }
    svc = _write_status(tmp_path, data)
    records: list[FakeRecord] = []

    with pytest.raises(SprintStateError, match="No review"):
        svc.advance(records)


def test_advance_auto_pick_falls_back_to_status_data_when_records_empty(
    tmp_path: Path,
):
    """Regression for the multi-repo deployment where
    ``ProgressSyncService`` is rooted at the agent repo and can't
    hand us meaningful records, but the correctly-rooted
    ``SprintStateService`` can still pick the next story from its
    own status file.
    """
    data = {
        "current_sprint": {
            "review": ["story-top-of-review"],
            "in_progress": ["story-b"],
            "planned": ["story-c"],
            "completed": [],
        },
    }
    svc = _write_status(tmp_path, data)

    changes = svc.advance([], reason="auto-from-data")

    assert len(changes) == 1
    assert changes[0].story_key == "story-top-of-review"
    assert changes[0].from_status == "review"
    assert changes[0].to_status == "done"


def test_advance_auto_pick_prefers_review_over_in_progress_from_data(
    tmp_path: Path,
):
    data = {
        "current_sprint": {
            "in_progress": ["b"],
            "review": ["a"],
            "planned": ["c"],
            "completed": [],
        },
    }
    svc = _write_status(tmp_path, data)

    changes = svc.advance([])

    assert changes[0].story_key == "a"
    assert changes[0].from_status == "review"


def test_advance_auto_pick_from_data_raises_when_nothing_advanceable(
    tmp_path: Path,
):
    data = {
        "current_sprint": {
            "completed": ["done-only"],
            "in_progress": [],
            "planned": [],
            "review": [],
        },
    }
    svc = _write_status(tmp_path, data)

    with pytest.raises(SprintStateError, match="No review"):
        svc.advance([])
