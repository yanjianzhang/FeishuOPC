"""Unit tests for ``RoleArtifactWriter``.

Scope
-----
This is the narrow specialist-write tool that lives at the bottom of
the trust chain (below tech-lead). We test its gates hard because if
any one of them leaks, a non-TL agent gains unintended write power:

- containment / ``..`` escape
- forbidden path segments (.env, secrets/, .git/, .pem, .key)
- size cap
- secret scanner hook-up
- empty-arg rejections
- ``try_handle`` dispatch + error-wrapping shape
- audit log shape
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from feishu_agent.team.role_artifact_writer import (
    ROLE_ARTIFACT_TOOL_SPECS,
    RoleArtifactEmptyError,
    RoleArtifactError,
    RoleArtifactOversizeError,
    RoleArtifactPathError,
    RoleArtifactSecretError,
    RoleArtifactWriter,
)
from feishu_agent.tools.code_write_service import CodeWriteAuditLog


def _make_writer(
    tmp_path: Path,
    *,
    role: str = "reviewer",
    project_id: str = "proj-a",
    audit_log: CodeWriteAuditLog | None = None,
    max_bytes: int = RoleArtifactWriter.DEFAULT_MAX_BYTES,
) -> RoleArtifactWriter:
    return RoleArtifactWriter(
        role_name=role,
        project_id=project_id,
        allowed_write_root=tmp_path,
        audit_log=audit_log,
        max_bytes=max_bytes,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_write_happy_path_creates_file(tmp_path: Path):
    writer = _make_writer(tmp_path)
    result = writer.write(
        path="3-1-review.md",
        content="# Review\nLooks good.\n",
        summary="Sprint 3-1 review report",
    )
    file_path = tmp_path / "3-1-review.md"
    assert file_path.read_text(encoding="utf-8").startswith("# Review")
    assert result.role == "reviewer"
    assert result.project_id == "proj-a"
    assert result.path == "3-1-review.md"
    assert result.bytes_written == len("# Review\nLooks good.\n".encode("utf-8"))
    assert result.summary == "Sprint 3-1 review report"


def test_write_creates_parent_dirs(tmp_path: Path):
    writer = _make_writer(tmp_path)
    writer.write(
        path="nested/sub/notes.md",
        content="ok",
        summary="nested note",
    )
    assert (tmp_path / "nested" / "sub" / "notes.md").read_text() == "ok"


def test_tool_specs_shape():
    assert len(ROLE_ARTIFACT_TOOL_SPECS) == 1
    spec = ROLE_ARTIFACT_TOOL_SPECS[0]
    assert spec.name == "write_role_artifact"


# ---------------------------------------------------------------------------
# Path containment / escape
# ---------------------------------------------------------------------------


def test_refuses_absolute_path(tmp_path: Path):
    writer = _make_writer(tmp_path)
    with pytest.raises(RoleArtifactPathError, match="absolute"):
        writer.write(path="/etc/passwd", content="x", summary="s")


def test_refuses_dotdot_escape(tmp_path: Path):
    writer = _make_writer(tmp_path)
    with pytest.raises(RoleArtifactPathError, match="escapes"):
        writer.write(path="../outside.md", content="x", summary="s")


def test_refuses_symlink_like_escape_with_parent(tmp_path: Path):
    writer = _make_writer(tmp_path)
    with pytest.raises(RoleArtifactPathError):
        writer.write(path="a/../../out.md", content="x", summary="s")


@pytest.mark.parametrize(
    "segment",
    [
        ".env",
        ".envrc",
        "secrets",
        ".git",
        "key.pem",
        "id.key",
    ],
)
def test_refuses_forbidden_segments(tmp_path: Path, segment: str):
    writer = _make_writer(tmp_path)
    with pytest.raises(RoleArtifactPathError, match="not allowed"):
        writer.write(path=f"docs/{segment}/x.md" if segment != "key.pem" and segment != "id.key" else segment, content="x", summary="s")


# ---------------------------------------------------------------------------
# Size cap
# ---------------------------------------------------------------------------


def test_refuses_oversize(tmp_path: Path):
    writer = _make_writer(tmp_path, max_bytes=100)
    with pytest.raises(RoleArtifactOversizeError, match="hard cap"):
        writer.write(path="big.md", content="x" * 101, summary="s")


def test_allows_at_cap(tmp_path: Path):
    writer = _make_writer(tmp_path, max_bytes=100)
    result = writer.write(path="edge.md", content="x" * 100, summary="s")
    assert result.bytes_written == 100


# ---------------------------------------------------------------------------
# Secret scanning
# ---------------------------------------------------------------------------


def test_refuses_content_with_secret(tmp_path: Path):
    writer = _make_writer(tmp_path)
    # AWS access-key-id shape is caught by the shared scanner.
    payload = "Here is the key: AKIAIOSFODNN7EXAMPLE please rotate it."
    with pytest.raises(RoleArtifactSecretError, match="secret-shaped"):
        writer.write(path="leak.md", content=payload, summary="leak")


# ---------------------------------------------------------------------------
# Empty-arg rejection
# ---------------------------------------------------------------------------


def test_refuses_empty_path(tmp_path: Path):
    writer = _make_writer(tmp_path)
    with pytest.raises(RoleArtifactPathError, match="non-empty"):
        writer.write(path="   ", content="ok", summary="s")


def test_refuses_empty_content(tmp_path: Path):
    writer = _make_writer(tmp_path)
    with pytest.raises(RoleArtifactEmptyError, match="non-empty"):
        writer.write(path="empty.md", content="", summary="s")


def test_refuses_empty_summary(tmp_path: Path):
    writer = _make_writer(tmp_path)
    with pytest.raises(RoleArtifactError, match="summary"):
        writer.write(path="x.md", content="ok", summary="   ")


# ---------------------------------------------------------------------------
# ``try_handle`` dispatch
# ---------------------------------------------------------------------------


def test_try_handle_returns_none_on_wrong_tool(tmp_path: Path):
    writer = _make_writer(tmp_path)
    assert writer.try_handle("some_other_tool", {}) is None


def test_try_handle_happy_path(tmp_path: Path):
    writer = _make_writer(tmp_path)
    result = writer.try_handle(
        "write_role_artifact",
        {"path": "r.md", "content": "hello", "summary": "greet"},
    )
    assert result is not None
    assert result["path"] == "r.md"
    assert result["bytes_written"] == 5
    assert result["role"] == "reviewer"


def test_try_handle_wraps_errors_into_dict(tmp_path: Path):
    writer = _make_writer(tmp_path)
    result = writer.try_handle(
        "write_role_artifact",
        {"path": "../escape.md", "content": "x", "summary": "s"},
    )
    assert result is not None
    assert result["error"] == "ROLE_ARTIFACT_PATH_INVALID"
    assert "escapes" in result["message"]


def test_try_handle_oversize_wraps(tmp_path: Path):
    writer = _make_writer(tmp_path, max_bytes=10)
    result = writer.try_handle(
        "write_role_artifact",
        {"path": "big.md", "content": "x" * 11, "summary": "s"},
    )
    assert result is not None
    assert result["error"] == "ROLE_ARTIFACT_OVERSIZE"


def test_try_handle_secret_wraps(tmp_path: Path):
    writer = _make_writer(tmp_path)
    result = writer.try_handle(
        "write_role_artifact",
        {
            "path": "leak.md",
            "content": "bad AKIAIOSFODNN7EXAMPLE",
            "summary": "s",
        },
    )
    assert result is not None
    assert result["error"] == "ROLE_ARTIFACT_SECRET_DETECTED"


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def test_audit_log_records_write(tmp_path: Path):
    audit_root = tmp_path / "audit"
    audit = CodeWriteAuditLog(root=audit_root, trace_id="trace-xyz")
    write_root = tmp_path / "project" / "docs" / "reviews"
    write_root.mkdir(parents=True)
    writer = RoleArtifactWriter(
        role_name="reviewer",
        project_id="p",
        allowed_write_root=write_root,
        audit_log=audit,
    )
    writer.write(path="r.md", content="hello", summary="s-summary")

    audit_files = list(audit_root.glob("*.jsonl"))
    assert audit_files, "audit file should be created"
    lines = audit_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert lines, "audit file should have at least one line"
    record = json.loads(lines[-1])
    assert record["event"] == "role_artifact_write"
    assert record["role"] == "reviewer"
    assert record["project_id"] == "p"
    assert record["path"] == "r.md"
    assert record["bytes_written"] == 5
    assert record["summary"] == "s-summary"
    # trace id is encoded in filename (CodeWriteAuditLog convention)
    assert "trace-xyz" in audit_files[0].name


# ---------------------------------------------------------------------------
# Containment is strict on resolve (symlink test)
# ---------------------------------------------------------------------------


def test_symlink_escape_is_refused(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "inside"
    root.mkdir()
    # pre-create a symlink INSIDE root that points to outside
    link = root / "escape"
    link.symlink_to(outside, target_is_directory=True)

    writer = _make_writer(root)
    # writing through the symlink into outside should fail containment
    with pytest.raises(RoleArtifactPathError):
        writer.write(path="escape/hax.md", content="x", summary="s")
