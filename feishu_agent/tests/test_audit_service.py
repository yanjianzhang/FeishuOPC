from __future__ import annotations

from pathlib import Path

import pytest

from feishu_agent.team.audit_service import AuditService


def test_write_and_read_roundtrip(tmp_path: Path):
    svc = AuditService(log_dir=tmp_path / "logs")
    payload = {"event": "sprint_advance", "story": "1-1", "ok": True}

    rel_path = svc.write("trace-001", payload)

    assert rel_path
    result = svc.read("trace-001")
    assert result == payload


def test_read_nonexistent_returns_none(tmp_path: Path):
    svc = AuditService(log_dir=tmp_path / "logs")
    svc.log_dir.mkdir(parents=True, exist_ok=True)

    assert svc.read("no-such-trace") is None


def test_write_creates_parent_directories(tmp_path: Path):
    nested = tmp_path / "deep" / "nested" / "logs"
    svc = AuditService(log_dir=nested)

    svc.write("trace-nested", {"hello": "world"})

    assert nested.exists()
    assert svc.read("trace-nested") == {"hello": "world"}


def test_write_handles_unicode(tmp_path: Path):
    svc = AuditService(log_dir=tmp_path / "logs")
    payload = {"message": "推进 sprint 状态", "emoji": "🍇"}

    svc.write("trace-unicode", payload)

    result = svc.read("trace-unicode")
    assert result == payload


@pytest.mark.parametrize("bad_id", ["../etc/passwd", "../../foo", "/absolute", "has space", "semi;colon", ""])
def test_write_rejects_unsafe_trace_id(tmp_path: Path, bad_id: str):
    svc = AuditService(log_dir=tmp_path / "logs")
    with pytest.raises(ValueError, match="Invalid trace_id"):
        svc.write(bad_id, {"x": 1})


def test_write_returns_filename(tmp_path: Path):
    svc = AuditService(log_dir=tmp_path / "logs")
    result = svc.write("trace-001", {"ok": True})
    assert result == "trace-001.json"
