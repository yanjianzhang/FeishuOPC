from __future__ import annotations

import json
from pathlib import Path

import pytest

from feishu_agent.team.pending_action_service import PendingAction, PendingActionService


@pytest.fixture()
def pending_dir(tmp_path: Path) -> Path:
    return tmp_path / "pending"


@pytest.fixture()
def service(pending_dir: Path) -> PendingActionService:
    return PendingActionService(pending_dir)


def _make_action(**overrides) -> PendingAction:
    defaults = {
        "trace_id": "trace-001",
        "chat_id": "chat-abc",
        "role_name": "tech_lead",
        "action_type": "write_progress_sync",
        "action_args": {"module": "vineyard_module"},
    }
    defaults.update(overrides)
    return PendingAction(**defaults)


# ======================================================================
# PendingAction dataclass
# ======================================================================


def test_pending_action_auto_sets_created_at():
    action = _make_action()
    assert action.created_at != ""
    assert "T" in action.created_at


def test_pending_action_roundtrip_dict():
    action = _make_action(confirmation_message_id="msg-123")
    data = action.to_dict()
    restored = PendingAction.from_dict(data)
    assert restored.trace_id == action.trace_id
    assert restored.chat_id == action.chat_id
    assert restored.role_name == action.role_name
    assert restored.action_type == action.action_type
    assert restored.action_args == action.action_args
    assert restored.confirmation_message_id == "msg-123"
    assert restored.created_at == action.created_at


def test_pending_action_from_dict_handles_missing_fields():
    restored = PendingAction.from_dict({"trace_id": "t1", "chat_id": "c1"})
    assert restored.role_name == ""
    assert restored.action_type == ""
    assert restored.action_args == {}
    assert restored.confirmation_message_id is None


# ======================================================================
# save + load roundtrip
# ======================================================================


def test_save_creates_file(service: PendingActionService, pending_dir: Path):
    action = _make_action()
    path = service.save(action)
    assert path.exists()
    assert path.name == "trace-001.json"
    assert path.parent == pending_dir


def test_save_and_load_roundtrip(service: PendingActionService):
    action = _make_action(action_args={"story_key": "3-1", "to_status": "review"})
    service.save(action)
    loaded = service.load("trace-001")
    assert loaded is not None
    assert loaded.trace_id == "trace-001"
    assert loaded.action_args == {"story_key": "3-1", "to_status": "review"}


def test_load_nonexistent_returns_none(service: PendingActionService):
    assert service.load("no-such-trace") is None


def test_load_corrupt_json_returns_none(service: PendingActionService, pending_dir: Path):
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / "bad.json").write_text("{invalid json", encoding="utf-8")
    assert service.load("bad") is None


# ======================================================================
# delete
# ======================================================================


def test_delete_removes_file(service: PendingActionService, pending_dir: Path):
    action = _make_action()
    service.save(action)
    assert service.delete("trace-001") is True
    assert not (pending_dir / "trace-001.json").exists()


def test_delete_nonexistent_returns_false(service: PendingActionService):
    assert service.delete("ghost") is False


# ======================================================================
# load_by_chat_id
# ======================================================================


def test_load_by_chat_id_finds_match(service: PendingActionService):
    service.save(_make_action(trace_id="t1", chat_id="chat-x"))
    service.save(_make_action(trace_id="t2", chat_id="chat-y"))
    result = service.load_by_chat_id("chat-x")
    assert result is not None
    assert result.trace_id == "t1"


def test_load_by_chat_id_returns_none_when_no_match(service: PendingActionService):
    service.save(_make_action(trace_id="t1", chat_id="chat-x"))
    assert service.load_by_chat_id("chat-z") is None


def test_load_by_chat_id_empty_dir(service: PendingActionService):
    assert service.load_by_chat_id("chat-x") is None


def test_load_by_chat_id_skips_corrupt_files(service: PendingActionService, pending_dir: Path):
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / "corrupt.json").write_text("not valid", encoding="utf-8")
    service.save(_make_action(trace_id="good", chat_id="chat-target"))
    result = service.load_by_chat_id("chat-target")
    assert result is not None
    assert result.trace_id == "good"


# ======================================================================
# JSON serialization format
# ======================================================================


def test_saved_json_is_valid_and_readable(service: PendingActionService, pending_dir: Path):
    action = _make_action()
    path = service.save(action)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["trace_id"] == "trace-001"
    assert raw["chat_id"] == "chat-abc"
    assert raw["action_type"] == "write_progress_sync"


# ======================================================================
# safety: unsafe trace_id rejected
# ======================================================================


def test_save_rejects_unsafe_trace_id(service: PendingActionService):
    action = _make_action(trace_id="../escape")
    with pytest.raises(ValueError, match="Unsafe trace_id"):
        service.save(action)


def test_load_rejects_unsafe_trace_id(service: PendingActionService):
    assert service.load("../escape") is None


def test_delete_rejects_unsafe_trace_id(service: PendingActionService):
    assert service.delete("../escape") is False
