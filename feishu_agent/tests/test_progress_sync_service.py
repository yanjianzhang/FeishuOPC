from __future__ import annotations

import json

import pytest
from feishu_fastapi_sdk import BitableWriteFailure, BitableWriteResult

from feishu_agent.schemas.progress_sync import ProgressRecord, ProgressSyncRequest
from feishu_agent.tools.progress_sync_service import ProgressSyncService


class StubFeishuClient:
    async def request(self, method, path, *, json_body=None, access_token=None):  # pragma: no cover - exercised in tests
        if path.endswith("/fields"):
            return {
                "items": [
                    {
                        "field_id": "fld_external",
                        "field_name": "ExternalKey",
                        "type": 1,
                        "property": None,
                    }
                ]
            }
        if "/records/search" in path:
            return {
                "items": [
                    {
                        "record_id": "rec_1",
                        "fields": {
                            "ExternalKey": "exampleapp:story-1",
                            "任务描述": "任务：图片处理功能",
                            "进展": "进行中",
                        },
                    },
                    {
                        "record_id": "rec_2",
                        "fields": {
                            "ExternalKey": "exampleapp:story-2",
                            "任务描述": "任务：别的需求",
                            "进展": "待开始",
                        },
                    },
                ],
                "has_more": False,
                "page_token": None,
            }
        return {
            "items": []
        }

    async def upsert_rows(self, target, rows, *, access_token=None):  # pragma: no cover - exercised in tests
        return BitableWriteResult(
            created=1,
            failed=1,
            failures=[
                BitableWriteFailure(
                    code="ROW_FAILED",
                    message="failed to write row",
                    external_key="exampleapp:story-1",
                )
            ],
        )


def make_service() -> ProgressSyncService:
    service = object.__new__(ProgressSyncService)
    service.feishu_client = StubFeishuClient()
    service.default_bitable_app_token = None
    service.default_bitable_table_id = None
    service.default_bitable_view_id = None
    service.default_bitable_table_name = None
    service.repo_root = None
    return service


def test_route_command_preview_does_not_match_review():
    service = make_service()

    routed = service.route_command("preview vineyard", "preview")

    assert routed["normalized_action"] == "preview_sync"
    assert routed["filters"]["module"] == ["vineyard_module"]
    assert routed["filters"]["status"] is None


def test_map_rows_applies_field_value_mapping():
    service = make_service()
    adapter = {
        "field_mapping": {
            "external_key": "ExternalKey",
            "status": "进展",
            "title": "任务描述",
        },
        "field_value_mapping": {
            "status": {
                "planned": "待开始",
                "done": "已完成",
            }
        },
    }
    records = [
        ProgressRecord(
            external_key="exampleapp:test-story",
            project_id="exampleapp",
            record_type="story",
            native_key="test-story",
            status="planned",
            summary="summary",
            source_path="project_knowledge/_bmad-output/implementation-artifacts/sprint-status.yaml",
            title="A task",
        )
    ]

    rows = service.map_rows(adapter, records)

    assert rows == [{"ExternalKey": "exampleapp:test-story", "进展": "待开始", "任务描述": "A task"}]


def test_record_from_leaf_prefers_chinese_task_text_and_owner(tmp_path):
    service = make_service()
    service.repo_root = tmp_path
    artifact_path = tmp_path / "project_knowledge" / "_bmad-output" / "implementation-artifacts" / "8-3-flutter-auth-ui.md"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("# 8-3 Flutter Auth UI (登录/注册界面)\n", encoding="utf-8")

    record = service._record_from_leaf(
        adapter={
            "project_id": "exampleapp",
            "project_type": "bmad-story-tracker",
            "source_roots": {
                "artifact_dir": "project_knowledge/_bmad-output/implementation-artifacts",
            },
            "record_defaults": {"record_type": "story", "owner": "", "risk": ""},
            "status_mapping": {"planned": "planned"},
        },
        native_key="8-3-flutter-auth-ui",
        raw_status="planned",
        module=None,
        source_path="project_knowledge/_bmad-output/implementation-artifacts/sprint-status.yaml",
        status_data={},
    )

    assert "登录/注册界面" in record.title
    assert "待开始" in record.summary
    assert "建议执行角色" in record.summary
    assert record.owner == "体验设计师"


@pytest.mark.asyncio
async def test_execute_marks_partial_writes_as_not_ok(monkeypatch: pytest.MonkeyPatch):
    service = make_service()

    monkeypatch.setattr(
        service,
        "load_adapter",
        lambda project_id: {"field_mapping": {"external_key": "ExternalKey"}},
    )
    monkeypatch.setattr(service, "route_command", lambda command_text, mode: {"normalized_action": "write_sync", "filters": {}})
    monkeypatch.setattr(service, "select_sources", lambda adapter, action: [])
    monkeypatch.setattr(service, "read_records", lambda adapter, sources: [])
    monkeypatch.setattr(service, "dedupe_records", lambda records: records)
    monkeypatch.setattr(service, "apply_filters", lambda records, filters: records)
    monkeypatch.setattr(service, "map_rows", lambda adapter, records: [{"ExternalKey": "exampleapp:story-1"}])
    monkeypatch.setattr(
        service,
        "_resolve_write_auth",
        lambda request: ("app_token", "table_id", "user_token", False),
    )

    result = await service.execute(
        ProgressSyncRequest(
            project_id="exampleapp",
            command_text="sync pending",
            mode="write",
            auth_mode="user",
        )
    )

    assert result.ok is False
    assert result.write_result is not None
    assert result.write_result.failed == 1
    assert result.errors == []


@pytest.mark.asyncio
async def test_execute_saves_schema_snapshot_before_write(tmp_path, monkeypatch: pytest.MonkeyPatch):
    service = make_service()
    service.repo_root = tmp_path
    service.default_bitable_table_name = "task_management"
    service.default_bitable_app_token = "app_token"
    service.default_bitable_table_id = "table_id"

    monkeypatch.setattr(
        service,
        "load_adapter",
        lambda project_id: {"field_mapping": {"external_key": "ExternalKey"}},
    )
    monkeypatch.setattr(service, "route_command", lambda command_text, mode: {"normalized_action": "write_sync", "filters": {}})
    monkeypatch.setattr(service, "select_sources", lambda adapter, action: [])
    monkeypatch.setattr(service, "read_records", lambda adapter, sources: [])
    monkeypatch.setattr(service, "dedupe_records", lambda records: records)
    monkeypatch.setattr(service, "apply_filters", lambda records, filters: records)
    monkeypatch.setattr(service, "map_rows", lambda adapter, records: [{"ExternalKey": "exampleapp:story-1"}])
    monkeypatch.setattr(
        service,
        "_resolve_write_auth",
        lambda request: ("app_token", "table_id", "user_token", False),
    )

    result = await service.execute(
        ProgressSyncRequest(
            project_id="exampleapp",
            command_text="sync pending",
            mode="write",
            auth_mode="user",
        )
    )

    schema_path = tmp_path / ".larkagent" / "secrets" / "feishu_app" / "schemas" / "task_management.schema.json"
    assert schema_path.exists()
    payload = json.loads(schema_path.read_text(encoding="utf-8"))
    assert payload["table_name"] == "task_management"
    assert payload["app_token"] == "app_token"
    assert payload["table_id"] == "table_id"
    assert payload["fields"][0]["field_name"] == "ExternalKey"
    assert any(w["code"] == "BITABLE_SCHEMA_SNAPSHOT_READY" for w in result.warnings)


@pytest.mark.asyncio
async def test_read_bitable_schema_returns_live_fields_and_snapshot(tmp_path):
    service = make_service()
    service.repo_root = tmp_path
    service.default_bitable_table_name = "task_management"
    service.default_bitable_app_token = "app_token"
    service.default_bitable_table_id = "table_id"
    service.default_bitable_view_id = "view_id"

    payload = await service.read_bitable_schema(auth_mode="tenant")

    assert payload["table_name"] == "task_management"
    assert payload["view_id"] == "view_id"
    assert payload["fields"][0]["field_name"] == "ExternalKey"
    assert payload["schema_snapshot_created"] is True
    assert payload["schema_snapshot_path"] == ".larkagent/secrets/feishu_app/schemas/task_management.schema.json"


@pytest.mark.asyncio
async def test_read_bitable_rows_reads_existing_rows_from_feishu(tmp_path):
    service = make_service()
    service.repo_root = tmp_path
    service.default_bitable_table_name = "task_management"
    service.default_bitable_app_token = "app_token"
    service.default_bitable_table_id = "table_id"
    service.default_bitable_view_id = "view_id"

    payload = await service.read_bitable_rows(search_text="图片处理", limit=10, auth_mode="tenant")

    assert payload["table_name"] == "task_management"
    assert payload["view_id"] == "view_id"
    assert payload["row_count"] == 1
    assert payload["rows"][0]["record_id"] == "rec_1"
    assert payload["rows"][0]["fields"]["任务描述"] == "任务：图片处理功能"
