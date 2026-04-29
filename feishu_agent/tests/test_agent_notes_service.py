"""Unit tests for agent_notes_service.

Covers:
- append() writes a new AGENT_NOTES.md with the header.
- Newest-first ordering on prepend.
- Per-session cap enforced.
- Length cap raises AgentNoteOversizeError.
- Empty / whitespace note raises AgentNoteEmptyError.
- Disabled service raises AgentNoteDisabledError.
- Secret detection raises AgentNoteSecretError.
- read_recent returns notes in newest-first order, limited.
- render_notes_for_prompt produces stable markdown.
- Parser round-trips (append + read returns matching notes).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from feishu_agent.team.agent_notes_service import (
    AgentNoteDisabledError,
    AgentNoteEmptyError,
    AgentNoteLimitError,
    AgentNoteOversizeError,
    AgentNoteSecretError,
    AgentNotesService,
    render_notes_for_prompt,
)


def _svc(tmp_path: Path, **kw) -> AgentNotesService:
    return AgentNotesService(
        project_id="demo",
        project_root=tmp_path,
        **kw,
    )


def test_append_creates_file_with_header(tmp_path: Path):
    svc = _svc(tmp_path)
    entry = svc.append(role="tech_lead", note="remember X")
    content = svc.notes_path.read_text()
    assert "# Agent Notes" in content
    assert "remember X" in content
    assert entry.role == "tech_lead"
    assert entry.project_id == "demo"


def test_newest_first_ordering(tmp_path: Path):
    svc = _svc(tmp_path)
    svc.append(role="tech_lead", note="first")
    svc.append(role="tech_lead", note="second")
    recent = svc.read_recent()
    assert [n.note for n in recent] == ["second", "first"]


def test_per_session_cap(tmp_path: Path):
    svc = _svc(tmp_path, max_notes_per_session=2)
    svc.append(role="tech_lead", note="a")
    svc.append(role="tech_lead", note="b")
    with pytest.raises(AgentNoteLimitError):
        svc.append(role="tech_lead", note="c")


def test_per_role_session_counter_is_per_role(tmp_path: Path):
    svc = _svc(tmp_path, max_notes_per_session=1)
    svc.append(role="tech_lead", note="tl-note")
    # Different role has its own quota.
    svc.append(role="developer", note="dev-note")
    with pytest.raises(AgentNoteLimitError):
        svc.append(role="tech_lead", note="second")


def test_oversize_rejected(tmp_path: Path):
    svc = _svc(tmp_path)
    with pytest.raises(AgentNoteOversizeError):
        svc.append(role="tech_lead", note="x" * (AgentNotesService.MAX_NOTE_CHARS + 1))


def test_empty_rejected(tmp_path: Path):
    svc = _svc(tmp_path)
    with pytest.raises(AgentNoteEmptyError):
        svc.append(role="tech_lead", note="   ")


def test_disabled_service(tmp_path: Path):
    svc = _svc(tmp_path, enabled=False)
    with pytest.raises(AgentNoteDisabledError):
        svc.append(role="tech_lead", note="anything")


def test_secret_detection_blocks_write(tmp_path: Path):
    svc = _svc(tmp_path)
    # A realistic AWS access key pattern (not a real key). The scanner
    # flags it; our wrapper surface it as AgentNoteSecretError.
    bad = "remember to use AKIAIOSFODNN7EXAMPLE for deploys"
    with pytest.raises(AgentNoteSecretError):
        svc.append(role="tech_lead", note=bad)
    # File shouldn't have been created with the secret.
    if svc.notes_path.exists():
        assert "AKIAIOSFODNN7EXAMPLE" not in svc.notes_path.read_text()


def test_read_recent_respects_limit(tmp_path: Path):
    svc = _svc(tmp_path, max_notes_per_session=10)
    for i in range(5):
        svc.append(role="tech_lead", note=f"n{i}")
    out = svc.read_recent(limit=3)
    assert len(out) == 3
    assert out[0].note == "n4"


def test_read_recent_missing_file_returns_empty(tmp_path: Path):
    svc = _svc(tmp_path)
    assert svc.read_recent() == []


def test_render_notes_for_prompt_empty_returns_blank():
    assert render_notes_for_prompt([]) == ""


def test_render_notes_for_prompt_nonempty_renders_header(tmp_path: Path):
    svc = _svc(tmp_path)
    svc.append(role="tech_lead", note="hello world")
    notes = svc.read_recent()
    rendered = render_notes_for_prompt(notes)
    assert "## Project memory" in rendered
    assert "hello world" in rendered


def test_round_trip_parser(tmp_path: Path):
    svc = _svc(tmp_path)
    svc.append(role="tech_lead", note="plain note")
    # Round-trip via a FRESH service instance to prove parsing is
    # independent of the in-memory state.
    svc2 = AgentNotesService(project_id="demo", project_root=tmp_path)
    notes = svc2.read_recent()
    assert len(notes) == 1
    assert notes[0].role == "tech_lead"
    assert notes[0].note == "plain note"


def test_select_for_prompt_prefers_query_relevant_note(tmp_path: Path):
    svc = _svc(tmp_path, max_notes_per_session=10)
    svc.append(
        role="tech_lead",
        note="Postgres migration order must match replicas",
    )
    svc.append(
        role="tech_lead",
        note="Flutter release build on Windows must use --release to avoid OOM",
    )
    selected = svc.select_for_prompt(
        query="flutter windows release oom",
        limit=1,
        role="tech_lead",
    )
    assert len(selected) == 1
    assert "Flutter release build" in selected[0].note


def test_prepend_preserves_old_entries(tmp_path: Path):
    svc = _svc(tmp_path, max_notes_per_session=10)
    svc.append(role="tech_lead", note="old one")
    # New service, new session budget.
    svc2 = _svc(tmp_path, max_notes_per_session=10)
    svc2.append(role="tech_lead", note="new one")
    content = svc2.notes_path.read_text()
    # Both notes present; newer one appears earlier in the file.
    i_new = content.index("new one")
    i_old = content.index("old one")
    assert i_new < i_old
