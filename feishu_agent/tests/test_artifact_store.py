"""Unit tests for :mod:`feishu_agent.team.artifact_store` (A-3
Wave 1).

Covers:

* Roundtrip: write → read → list on a fresh store.
* Atomic write: forcing a partial write leaves the target intact.
* Concurrent writes of different artifact_ids do not lose data.
* Forward-compat: extra JSON keys are ignored on read.
* ``inbox/`` directory is materialised by the first write (T046
  contract, exercised here so Wave 2's TL wiring test doesn't have
  to re-derive it).
"""

from __future__ import annotations

import concurrent.futures
import json
import uuid
from dataclasses import asdict
from pathlib import Path

import pytest

from feishu_agent.team.artifact_store import (
    ArtifactNotFoundError,
    ArtifactStore,
    FileTouch,
    RoleArtifact,
    ToolCallRecord,
    truncate_preview,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_artifact(
    *,
    role_name: str = "repo_inspector",
    artifact_id: str | None = None,
    root_trace_id: str = "trace-root-1",
    success: bool = True,
    tool_calls: list[ToolCallRecord] | None = None,
    files_touched: list[FileTouch] | None = None,
) -> RoleArtifact:
    aid = artifact_id or uuid.uuid4().hex
    return RoleArtifact(
        artifact_id=aid,
        parent_trace_id=root_trace_id,
        root_trace_id=root_trace_id,
        role_name=role_name,
        task="inspect repo",
        acceptance_criteria="report 3 files",
        started_at=1700000000000,
        completed_at=1700000001000,
        duration_ms=1000,
        success=success,
        stop_reason="complete" if success else "error",
        tool_calls=tool_calls or [],
        files_touched=files_touched or [],
        risk_score=0.1,
        token_usage={"input": 100, "output": 50, "total_tokens": 150},
        output_text="OK",
        error_message=None,
        worktree_fallback=False,
        concurrency_group="repo_inspector",
    )


# ---------------------------------------------------------------------------
# Roundtrip / layout
# ---------------------------------------------------------------------------


def test_write_returns_expected_path(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    art = _make_artifact(role_name="sprint_planner")

    target = store.write(art)

    expected = (
        tmp_path
        / "teams"
        / art.root_trace_id
        / "artifacts"
        / f"sprint_planner-{art.artifact_id}.json"
    )
    assert target == expected
    assert target.exists(), "artifact must be on disk after write"


def test_write_creates_inbox_dir(tmp_path: Path) -> None:
    """T046: the team layout is ``artifacts/``, ``pending/``,
    ``inbox/``, ``transcript.jsonl``. The store owns inbox creation
    so an operator doing ``ls teams/{trace}/`` after the first
    dispatch sees the full shape."""
    store = ArtifactStore(tmp_path)
    art = _make_artifact()

    store.write(art)

    inbox = tmp_path / "teams" / art.root_trace_id / "inbox"
    assert inbox.is_dir()


def test_write_read_roundtrip(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    art = _make_artifact(
        tool_calls=[
            ToolCallRecord(
                tool_name="read_sprint_status",
                arguments_preview="{}",
                result_preview='{"goal": "ship"}',
                duration_ms=12,
                is_error=False,
                started_at=1700000000100,
            )
        ],
        files_touched=[
            FileTouch(
                path="docs/repo-analysis/2026-04-23.md",
                kind="write",
                bytes_written=1234,
            )
        ],
    )

    store.write(art)
    loaded = store.read(art.root_trace_id, art.artifact_id)

    # Dataclass equality checks every field.
    assert loaded == art


def test_list_returns_every_artifact(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    root = "trace-root-list"
    aids = [
        store.write(
            _make_artifact(
                role_name=role, artifact_id=f"id-{i}", root_trace_id=root
            )
        )
        for i, role in enumerate(("repo_inspector", "sprint_planner", "qa_tester"))
    ]

    loaded = store.list(root)

    assert len(loaded) == 3
    names = {a.role_name for a in loaded}
    assert names == {"repo_inspector", "sprint_planner", "qa_tester"}
    # Every file on disk mapped back into a RoleArtifact.
    assert all(isinstance(a, RoleArtifact) for a in loaded)
    assert len(aids) == 3


def test_read_missing_raises(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    with pytest.raises(ArtifactNotFoundError):
        store.read("trace-nobody", "nope")


# ---------------------------------------------------------------------------
# Atomicity — ``.tmp + rename`` semantics
# ---------------------------------------------------------------------------


def test_write_uses_atomic_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After a successful write, no ``.tmp`` sibling should remain,
    proving we rename (not copy). This is the best we can do in a
    unit test without killing a subprocess mid-write."""
    store = ArtifactStore(tmp_path)
    art = _make_artifact()

    store.write(art)

    art_dir = tmp_path / "teams" / art.root_trace_id / "artifacts"
    leftovers = list(art_dir.glob("*.tmp"))
    assert leftovers == [], f"stale tmp file left behind: {leftovers}"


def test_partial_write_does_not_overwrite_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulate a crash between tmp.write_text and tmp.replace — the
    target should remain whatever was there before (or absent).
    """
    store = ArtifactStore(tmp_path)
    first = _make_artifact(role_name="qa_tester", artifact_id="aid-1")
    store.write(first)

    target_path = (
        tmp_path
        / "teams"
        / first.root_trace_id
        / "artifacts"
        / f"qa_tester-{first.artifact_id}.json"
    )
    original_bytes = target_path.read_bytes()

    # Monkeypatch Path.replace on the tmp so the "rename" fails.
    from pathlib import Path as _RealPath

    real_replace = _RealPath.replace

    def _boom(self: _RealPath, target: _RealPath) -> _RealPath:  # type: ignore[override]
        if str(self).endswith(".tmp"):
            raise OSError("simulated rename failure")
        return real_replace(self, target)

    monkeypatch.setattr(_RealPath, "replace", _boom, raising=True)

    second = _make_artifact(role_name="qa_tester", artifact_id="aid-1")
    second.output_text = "REPLACED"
    with pytest.raises(OSError):
        store.write(second)

    # Target file is still the original payload.
    assert target_path.read_bytes() == original_bytes


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_writes_of_distinct_ids_all_land(tmp_path: Path) -> None:
    """Each artifact_id is unique, so writes should not collide. We
    fire 20 parallel writes into a thread pool and assert all 20
    land intact."""

    store = ArtifactStore(tmp_path)
    root = "trace-parallel"
    N = 20

    def _one(i: int) -> Path:
        art = _make_artifact(
            role_name="repo_inspector",
            artifact_id=f"par-{i:03d}",
            root_trace_id=root,
        )
        return store.write(art)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(_one, range(N)))

    assert len(paths) == N
    assert len({p for p in paths}) == N, "every write must produce a unique path"

    loaded = store.list(root)
    assert len(loaded) == N
    aids = sorted(a.artifact_id for a in loaded)
    assert aids == sorted(f"par-{i:03d}" for i in range(N))


# ---------------------------------------------------------------------------
# Forward-compat on read
# ---------------------------------------------------------------------------


def test_read_ignores_unknown_future_fields(tmp_path: Path) -> None:
    """A file written by a future version with extra top-level keys
    must still load on the current code. This future-proofs the
    envelope — bumping ``schema_version`` + adding fields shouldn't
    break older agents in a mixed-deployment window."""

    store = ArtifactStore(tmp_path)
    art = _make_artifact()
    path = store.write(art)

    # Inject an unknown key into the on-disk JSON.
    data = json.loads(path.read_text(encoding="utf-8"))
    data["future_key"] = {"nested": [1, 2, 3]}
    data["schema_version"] = 99
    path.write_text(json.dumps(data), encoding="utf-8")

    loaded = store.read(art.root_trace_id, art.artifact_id)

    # Core fields intact; schema_version reflects what was on disk.
    assert loaded.artifact_id == art.artifact_id
    assert loaded.schema_version == 99
    # ``future_key`` is dropped silently (it isn't a dataclass field).


def test_read_skips_corrupt_files_in_list(tmp_path: Path) -> None:
    """``list()`` must not crash if one file on disk is unreadable;
    it should skip with a warning and return the remainder."""

    store = ArtifactStore(tmp_path)
    root = "trace-corrupt"
    good = _make_artifact(role_name="repo_inspector", artifact_id="g1", root_trace_id=root)
    store.write(good)

    # Drop a malformed file next to the good one.
    bad = (
        tmp_path
        / "teams"
        / root
        / "artifacts"
        / "broken-x.json"
    )
    bad.write_text("{not valid json", encoding="utf-8")

    loaded = store.list(root)
    assert len(loaded) == 1
    assert loaded[0].artifact_id == "g1"


# ---------------------------------------------------------------------------
# ``truncate_preview`` helper
# ---------------------------------------------------------------------------


def test_truncate_preview_respects_limit() -> None:
    payload = {"k": "v" * 2000}
    out = truncate_preview(payload, 100)
    assert len(out) == 100
    assert out.endswith("...")


def test_truncate_preview_returns_full_when_under_limit() -> None:
    out = truncate_preview({"a": 1}, 1000)
    # Short payloads come back without ellipsis.
    assert out == '{"a": 1}'


def test_truncate_preview_falls_back_on_non_serialisable() -> None:
    """Functions aren't JSON-serialisable by default; rather than
    lose the record, we fall back to ``repr()`` so something
    diagnostic ends up in the envelope."""

    def f() -> None: ...

    out = truncate_preview({"fn": f}, 200)
    # ``json.dumps(default=str)`` calls ``str(f)``; expect a
    # representation with "function" in it either way.
    assert "function" in out or "<" in out


def test_roundtrip_preserves_artifact_dataclass_shape(tmp_path: Path) -> None:
    """Regression guard: ``asdict`` + ``json.dumps`` + reverse must
    not silently drop dataclass-level defaults (e.g. the empty list
    fields) when a field was written with its default value."""

    store = ArtifactStore(tmp_path)
    art = _make_artifact()  # tool_calls / files_touched default to []
    store.write(art)
    loaded = store.read(art.root_trace_id, art.artifact_id)
    # Compare dicts to catch any field-count drift.
    assert asdict(loaded) == asdict(art)
