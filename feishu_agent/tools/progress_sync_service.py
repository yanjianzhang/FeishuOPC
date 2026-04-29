from __future__ import annotations

import json
import re
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from feishu_fastapi_sdk import (
    BitableTarget,
    FeishuAuthConfig,
    FeishuClient,
    FeishuConfigError,
)

from feishu_agent.config import get_settings
from feishu_agent.schemas.progress_sync import (
    ProgressRecord,
    ProgressSyncRequest,
    ProgressSyncResponse,
)

settings = get_settings()

CANONICAL_STATUSES = {
    "planned",
    "in-progress",
    "review",
    "done",
    "blocked",
    "unknown",
}
LIST_STATUS_MAP = {
    "completed": "done",
    "in_progress": "in-progress",
    "review": "review",
    "blocked": "blocked",
    "planned": "planned",
}
CHINESE_STATUS_LABELS = {
    "planned": "待开始",
    "in-progress": "进行中",
    "review": "评审中",
    "done": "已完成",
    "blocked": "已阻塞",
    "unknown": "状态未知",
}
MODULE_DISPLAY_NAMES = {
    "vineyard_module": "葡萄庄园模块",
    "current_sprint": "当前冲刺",
    "development_status": "开发状态",
    "project_summary": "项目概览",
}
ROLE_DISPLAY_NAMES = {
    "reviewer": "审查员",
    "researcher": "研究员",
    "sprint_planner": "冲刺规划师",
    "repo_inspector": "仓库巡检员",
    "prd_writer": "PRD 写手",
    "ux_designer": "体验设计师",
    "progress_sync": "进度同步专员",
    "qa_tester": "QA 测试员",
    "spec_linker": "规格链接员",
}
OWNER_KEYWORD_RULES = [
    (("sync", "bitable", "feishu", "progress"), "progress_sync"),
    (("review", "qa", "test", "verify", "smoke"), "qa_tester"),
    (("spec", "brief", "epic", "story"), "spec_linker"),
    (("prd", "requirement", "acceptance", "copy"), "prd_writer"),
    (("ui", "ux", "page", "screen", "tab", "widget", "login", "register", "profile"), "ux_designer"),
    (("research", "spike", "etl", "data", "analysis", "narrative"), "researcher"),
    (("sprint", "roadmap", "plan", "milestone"), "sprint_planner"),
    (("deploy", "backend", "api", "database", "auth", "service", "router", "runtime", "refactor"), "repo_inspector"),
]


class AdapterError(Exception):
    pass


class ProgressSyncService:
    def __init__(
        self,
        feishu_client: FeishuClient | None = None,
        *,
        default_bitable_app_token: str | None = None,
        default_bitable_table_id: str | None = None,
        default_bitable_view_id: str | None = None,
        default_bitable_table_name: str | None = None,
    ) -> None:
        self.feishu_client = feishu_client or FeishuClient(
            FeishuAuthConfig(
                app_id=settings.feishu_bot_app_id or "",
                app_secret=settings.feishu_bot_app_secret or "",
            )
        )
        self.default_bitable_app_token = default_bitable_app_token
        self.default_bitable_table_id = default_bitable_table_id
        self.default_bitable_view_id = default_bitable_view_id
        self.default_bitable_table_name = default_bitable_table_name
        self.repo_root = self._discover_repo_root()

    @contextmanager
    def _internal_token_kind(self, token_kind: str):
        previous = getattr(self.feishu_client, "default_internal_token_kind", None)
        if previous is None:
            yield
            return
        self.feishu_client.default_internal_token_kind = token_kind
        try:
            yield
        finally:
            self.feishu_client.default_internal_token_kind = previous

    @staticmethod
    def _looks_like_repo_root(path: Path) -> bool:
        if (path / ".larkagent").exists():
            return True
        return (path / "project-adapters").exists() and (path / "feishu_agent").exists()

    def _discover_repo_root(self) -> Path:
        candidates: list[Path] = []
        if settings.app_repo_root:
            candidates.append(Path(settings.app_repo_root).expanduser())
        candidates.append(Path.cwd())
        candidates.extend(Path(__file__).resolve().parents)

        seen: set[Path] = set()
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if self._looks_like_repo_root(resolved):
                return resolved

        raise AdapterError(
            "Unable to locate progress sync repository root. "
            "Set APP_REPO_ROOT to a directory containing project-adapters/ and feishu_agent/."
        )

    @staticmethod
    def _contains_term(text: str, term: str) -> bool:
        return re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", text) is not None

    def _adapter_path(self, project_id: str) -> Path:
        return self.repo_root / "project-adapters" / f"{project_id}-progress.json"

    def load_adapter(self, project_id: str) -> dict[str, Any]:
        path = self._adapter_path(project_id)
        if not path.exists():
            raise AdapterError(f"Project adapter not found: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def route_command(self, command_text: str, mode: str) -> dict[str, Any]:
        text = command_text.strip().lower()
        filters: dict[str, list[str] | None] = {"module": None, "status": None}
        action = "preview_sync" if mode == "preview" else "write_sync"

        if "待规划" in command_text or self._contains_term(text, "pending"):
            action = "list_pending"
            filters["status"] = ["planned"]
        if self._contains_term(text, "vineyard"):
            filters["module"] = ["vineyard_module"]
        if self._contains_term(text, "review"):
            filters["status"] = ["review"]
        elif self._contains_term(text, "done") or "已完成" in command_text:
            filters["status"] = ["done"]
        elif self._contains_term(text, "in-progress") or "进行中" in command_text:
            filters["status"] = ["in-progress"]

        return {
            "normalized_action": action,
            "filters": filters,
            "execution": {
                "mode": mode,
                "dry_run": mode == "preview",
            },
        }

    def select_sources(self, adapter: dict[str, Any], action: str) -> list[dict[str, Any]]:
        roots = adapter.get("source_roots") or {}
        sources = []
        status_file = roots.get("status_file")
        if status_file:
            sources.append({"kind": "status_file", "path": status_file, "required": True})
        if action in {"preview_sync", "write_sync", "show_module_status"}:
            for kind in ("artifact_dir", "specs_dir"):
                if roots.get(kind):
                    sources.append({"kind": kind, "path": roots[kind], "required": False})
        if not sources:
            raise AdapterError("Project adapter has no readable sources.")
        return sources

    def _resolve(self, relative_path: str) -> Path:
        return self.repo_root / relative_path

    def _schema_snapshot_path(self, table_name: str) -> Path:
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", table_name.strip()) or "unknown_table"
        return self.repo_root / ".larkagent" / "secrets" / "feishu_app" / "schemas" / f"{safe_name}.schema.json"

    @staticmethod
    def _bitable_rows_path(
        *,
        app_token: str,
        table_id: str,
        page_size: int,
        page_token: str | None = None,
    ) -> str:
        path = f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/search?page_size={page_size}"
        if page_token:
            path += f"&page_token={page_token}"
        return path

    @staticmethod
    def _bitable_row_matches_search(fields: dict[str, Any], search_text: str | None) -> bool:
        if not search_text:
            return True
        haystack = json.dumps(fields, ensure_ascii=False, default=str).lower()
        return search_text.strip().lower() in haystack

    async def read_bitable_schema(
        self,
        *,
        table_name: str | None = None,
        app_token: str | None = None,
        table_id: str | None = None,
        access_token: str | None = None,
        auth_mode: str = "tenant",
    ) -> dict[str, Any]:
        resolved_table_name = table_name or self.default_bitable_table_name
        resolved_app_token = app_token or self.default_bitable_app_token
        resolved_table_id = table_id or self.default_bitable_table_id
        if not resolved_app_token or not resolved_table_id:
            raise FeishuConfigError("Feishu Bitable target is not configured.")

        token_kind = "tenant" if auth_mode == "tenant" else ("app" if access_token is None else "user")
        with self._internal_token_kind(token_kind):
            snapshot_path, snapshot_created = await self.ensure_bitable_schema_snapshot(
                table_name=resolved_table_name,
                app_token=resolved_app_token,
                table_id=resolved_table_id,
                access_token=access_token,
            )
            data = await self.feishu_client.request(
                "GET",
                f"/open-apis/bitable/v1/apps/{resolved_app_token}/tables/{resolved_table_id}/fields",
                access_token=access_token,
            )
        return {
            "table_name": resolved_table_name,
            "app_token": resolved_app_token,
            "table_id": resolved_table_id,
            "view_id": self.default_bitable_view_id,
            "fields": data.get("items") or [],
            "schema_snapshot_path": None
            if snapshot_path is None
            else str(snapshot_path.relative_to(self.repo_root)),
            "schema_snapshot_created": snapshot_created,
        }

    async def read_bitable_rows(
        self,
        *,
        table_name: str | None = None,
        app_token: str | None = None,
        table_id: str | None = None,
        view_id: str | None = None,
        field_names: list[str] | None = None,
        search_text: str | None = None,
        limit: int = 20,
        page_token: str | None = None,
        access_token: str | None = None,
        auth_mode: str = "tenant",
    ) -> dict[str, Any]:
        resolved_table_name = table_name or self.default_bitable_table_name
        resolved_app_token = app_token or self.default_bitable_app_token
        resolved_table_id = table_id or self.default_bitable_table_id
        resolved_view_id = view_id or self.default_bitable_view_id
        if not resolved_app_token or not resolved_table_id:
            raise FeishuConfigError("Feishu Bitable target is not configured.")

        normalized_limit = max(1, min(limit, 100))
        normalized_field_names = [name for name in (field_names or []) if str(name).strip()]
        token_kind = "tenant" if auth_mode == "tenant" else ("app" if access_token is None else "user")
        matched_rows: list[dict[str, Any]] = []
        current_page_token = page_token
        has_more = False
        next_page_token = None

        with self._internal_token_kind(token_kind):
            for _ in range(5):
                data = await self.feishu_client.request(
                    "POST",
                    self._bitable_rows_path(
                        app_token=resolved_app_token,
                        table_id=resolved_table_id,
                        page_size=normalized_limit,
                        page_token=current_page_token,
                    ),
                    json_body={
                        key: value
                        for key, value in {
                            "view_id": resolved_view_id,
                            "field_names": normalized_field_names or None,
                        }.items()
                        if value is not None
                    },
                    access_token=access_token,
                )
                items = data.get("items") or []
                has_more = bool(data.get("has_more"))
                next_page_token = data.get("page_token")
                for item in items:
                    fields = item.get("fields") or {}
                    if not isinstance(fields, dict):
                        continue
                    if not self._bitable_row_matches_search(fields, search_text):
                        continue
                    matched_rows.append(
                        {
                            "record_id": item.get("record_id"),
                            "fields": fields,
                        }
                    )
                if len(matched_rows) >= normalized_limit or not has_more or not next_page_token:
                    break
                current_page_token = str(next_page_token)

        rows = matched_rows[:normalized_limit]
        return {
            "table_name": resolved_table_name,
            "app_token": resolved_app_token,
            "table_id": resolved_table_id,
            "view_id": resolved_view_id,
            "field_names": normalized_field_names,
            "search_text": search_text,
            "row_count": len(rows),
            "rows": rows,
            "has_more": has_more,
            "next_page_token": next_page_token if has_more else None,
        }

    async def ensure_bitable_schema_snapshot(
        self,
        *,
        table_name: str | None,
        app_token: str,
        table_id: str,
        access_token: str | None,
    ) -> tuple[Path | None, bool]:
        resolved_name = (table_name or self.default_bitable_table_name or "").strip()
        if not resolved_name:
            return None, False

        snapshot_path = self._schema_snapshot_path(resolved_name)
        if snapshot_path.exists():
            return snapshot_path, False

        data = await self.feishu_client.request(
            "GET",
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            access_token=access_token,
        )
        payload = {
            "table_name": resolved_name,
            "app_token": app_token,
            "table_id": table_id,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "fields": data.get("items") or [],
        }
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return snapshot_path, True

    def _normalize_status(self, adapter: dict[str, Any], raw_status: str) -> str:
        mapping = adapter.get("status_mapping") or {}
        normalized = mapping.get(raw_status, mapping.get(raw_status.replace("_", "-"), raw_status))
        normalized = normalized.replace("_", "-") if isinstance(normalized, str) else "unknown"
        if normalized not in CANONICAL_STATUSES:
            return "unknown"
        return normalized

    @staticmethod
    def _humanize(key: str) -> str:
        return key.replace("_", " ").replace("-", " ").strip().title()

    @staticmethod
    def _contains_cjk(text: str | None) -> bool:
        return bool(text and re.search(r"[\u3400-\u9fff]", text))

    @staticmethod
    def _strip_story_prefix(title: str) -> str:
        return re.sub(r"^Story\s+[A-Za-z0-9.-]+:\s*", "", title).strip()

    def _read_artifact_title(self, artifact_path: str | None) -> str | None:
        if not artifact_path:
            return None
        path = self.repo_root / artifact_path
        if not path.exists():
            return None
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("#"):
                return self._strip_story_prefix(line.lstrip("#").strip())
        return None

    def _derive_title(self, native_key: str, artifact_path: str | None) -> str:
        artifact_title = self._read_artifact_title(artifact_path)
        if artifact_title:
            if self._contains_cjk(artifact_title):
                return artifact_title
            return f"任务：{artifact_title}"
        return f"任务：{self._humanize(native_key)}"

    def _infer_owner(self, *, native_key: str, title: str, module: str | None) -> str:
        haystack = " ".join(filter(None, [native_key, title, module])).lower()
        for keywords, role_key in OWNER_KEYWORD_RULES:
            if any(keyword in haystack for keyword in keywords):
                return ROLE_DISPLAY_NAMES.get(role_key, role_key)
        return ROLE_DISPLAY_NAMES["repo_inspector"]

    def _build_chinese_summary(
        self,
        *,
        native_key: str,
        title: str,
        status: str,
        module: str | None,
        owner: str,
    ) -> str:
        status_label = CHINESE_STATUS_LABELS.get(status, status)
        module_label = MODULE_DISPLAY_NAMES.get(module or "", self._humanize(module) if module else "当前冲刺")
        return (
            f"任务「{title}」当前状态为{status_label}。"
            f"所属范围：{module_label}。"
            f"建议执行角色：{owner}。"
            f"任务标识：{native_key}。"
        )

    def _derive_paths(
        self,
        adapter: dict[str, Any],
        native_key: str,
        module: str | None,
        status_data: dict[str, Any],
    ) -> tuple[str | None, str | None]:
        roots = adapter.get("source_roots") or {}
        artifact_path = None
        artifact_dir = roots.get("artifact_dir")
        if artifact_dir:
            candidate = self._resolve(f"{artifact_dir}/{native_key}.md")
            if candidate.exists():
                artifact_path = str(candidate.relative_to(self.repo_root))

        spec_path = None
        if module == "vineyard_module":
            vineyard_spec = (
                (status_data.get("current_sprint") or {})
                .get("vineyard_module", {})
                .get("spec")
            )
            if vineyard_spec:
                spec_path = str(vineyard_spec).rstrip("/")
        return spec_path, artifact_path

    def _record_from_leaf(
        self,
        *,
        adapter: dict[str, Any],
        native_key: str,
        raw_status: str,
        module: str | None,
        source_path: str,
        status_data: dict[str, Any],
        source_kind: str = "status_file",
    ) -> ProgressRecord:
        normalized_status = self._normalize_status(adapter, raw_status)
        spec_path, artifact_path = self._derive_paths(adapter, native_key, module, status_data)
        title = self._derive_title(native_key, artifact_path)
        owner = self._infer_owner(native_key=native_key, title=title, module=module)
        return ProgressRecord(
            external_key=f"{adapter['project_id']}:{native_key}",
            project_id=adapter["project_id"],
            project_type=adapter.get("project_type"),
            record_type=(adapter.get("record_defaults") or {}).get("record_type", "story"),
            native_key=native_key,
            story_key=native_key,
            module=module,
            status=normalized_status,
            raw_status=raw_status,
            title=title,
            summary=self._build_chinese_summary(
                native_key=native_key,
                title=title,
                status=normalized_status,
                module=module,
                owner=owner,
            ),
            owner=owner or (adapter.get("record_defaults") or {}).get("owner", ""),
            risk=(adapter.get("record_defaults") or {}).get("risk", ""),
            spec_path=spec_path,
            artifact_path=artifact_path,
            source_path=source_path,
            source_kind=source_kind,
            updated_at=None,
            tags=[module] if module else [],
        )

    def _collect_records_from_mapping(
        self,
        *,
        adapter: dict[str, Any],
        mapping: dict[str, Any],
        module: str | None,
        source_path: str,
        status_data: dict[str, Any],
    ) -> list[ProgressRecord]:
        records: list[ProgressRecord] = []
        for key, value in mapping.items():
            if key in {"spec", "name", "goal", "start_date"}:
                continue
            if isinstance(value, str):
                normalized = self._normalize_status(adapter, value)
                if normalized != "unknown":
                    records.append(
                        self._record_from_leaf(
                            adapter=adapter,
                            native_key=key,
                            raw_status=value,
                            module=module,
                            source_path=source_path,
                            status_data=status_data,
                        )
                    )
            elif isinstance(value, dict):
                next_module = key if key in (adapter.get("module_mapping") or {}) else module
                records.extend(
                    self._collect_records_from_mapping(
                        adapter=adapter,
                        mapping=value,
                        module=next_module,
                        source_path=source_path,
                        status_data=status_data,
                    )
                )
            elif isinstance(value, list) and key in LIST_STATUS_MAP:
                raw_status = LIST_STATUS_MAP[key]
                for item in value:
                    if isinstance(item, str):
                        records.append(
                            self._record_from_leaf(
                                adapter=adapter,
                                native_key=item,
                                raw_status=raw_status,
                                module=module,
                                source_path=source_path,
                                status_data=status_data,
                            )
                        )
        return records

    def read_records(self, adapter: dict[str, Any], sources: list[dict[str, Any]]) -> list[ProgressRecord]:
        status_source = next((source for source in sources if source["kind"] == "status_file"), None)
        if not status_source:
            raise AdapterError("No status_file source declared for record extraction.")
        path = self._resolve(status_source["path"])
        if not path.exists():
            raise AdapterError(f"Status file missing: {status_source['path']}")
        status_data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        source_path = str(path.relative_to(self.repo_root))
        return self._collect_records_from_mapping(
            adapter=adapter,
            mapping=status_data,
            module=None,
            source_path=source_path,
            status_data=status_data,
        )

    @staticmethod
    def dedupe_records(records: list[ProgressRecord]) -> list[ProgressRecord]:
        deduped: dict[str, ProgressRecord] = {}
        for record in records:
            existing = deduped.get(record.external_key)
            if existing is None:
                deduped[record.external_key] = record
                continue
            if not existing.module and record.module:
                deduped[record.external_key] = record
                continue
            if not existing.artifact_path and record.artifact_path:
                deduped[record.external_key] = record
        return list(deduped.values())

    @staticmethod
    def apply_filters(
        records: list[ProgressRecord],
        filters: dict[str, list[str] | None],
    ) -> list[ProgressRecord]:
        result = records
        modules = filters.get("module") or []
        statuses = filters.get("status") or []
        if modules:
            result = [record for record in result if record.module in modules]
        if statuses:
            result = [record for record in result if record.status in statuses]
        return result

    def map_rows(self, adapter: dict[str, Any], records: list[ProgressRecord]) -> list[dict[str, Any]]:
        field_mapping = adapter.get("field_mapping") or {}
        field_value_mapping = adapter.get("field_value_mapping") or {}
        rows: list[dict[str, Any]] = []
        for record in records:
            record_data = record.model_dump()
            row: dict[str, Any] = {}
            for canonical_field, target_field in field_mapping.items():
                value = record_data.get(canonical_field)
                if value is None:
                    continue
                value_map = field_value_mapping.get(canonical_field)
                if isinstance(value_map, dict):
                    value = value_map.get(value, value)
                row[target_field] = value
            rows.append(row)
        return rows

    @staticmethod
    def summarize(records: list[ProgressRecord]) -> dict[str, Any]:
        counter = Counter(record.status for record in records)
        return {
            "total": len(records),
            "by_status": dict(sorted(counter.items())),
        }

    @staticmethod
    def _build_message(
        *,
        project_id: str,
        mode: str,
        summary: dict[str, Any],
        write_result: Any,
        errors: list[dict[str, Any]],
    ) -> str:
        if errors:
            return f"[{project_id}] sync failed: {errors[0]['message']}"
        if mode == "preview":
            return (
                f"[{project_id}] previewed {summary['total']} records. "
                f"Status split: {summary['by_status']}"
            )
        assert write_result is not None
        return (
            f"[{project_id}] synced {summary['total']} records: "
            f"{write_result.created} created, {write_result.updated} updated, "
            f"{write_result.skipped} skipped, {write_result.failed} failed."
        )

    @staticmethod
    def _extract_app_token_from_base_url(base_url: str | None) -> str | None:
        if not base_url:
            return None
        match = re.search(r"/(?:base|apps)/([A-Za-z0-9]+)", base_url)
        return match.group(1) if match else None

    @staticmethod
    def _looks_like_user_access_token(value: str | None) -> bool:
        return bool(value and value.startswith("u-"))

    def _resolve_write_auth(
        self,
        request: ProgressSyncRequest,
    ) -> tuple[str | None, str | None, str | None, bool]:
        request_app_token = request.bitable_app_token
        settings_app_token = self.default_bitable_app_token or settings.feishu_default_bitable_app_token
        configured_app_token = request_app_token or settings_app_token
        legacy_user_access_token = None
        if self._looks_like_user_access_token(request_app_token):
            legacy_user_access_token = configured_app_token
            configured_app_token = None
        elif self._looks_like_user_access_token(settings_app_token):
            legacy_user_access_token = settings_app_token

        app_token = configured_app_token or self._extract_app_token_from_base_url(settings.feishu_bitable_base_url)
        table_id = request.bitable_table_id or self.default_bitable_table_id or settings.feishu_default_bitable_table_id
        user_access_token = (
            request.user_access_token
            or settings.feishu_default_user_access_token
            or legacy_user_access_token
        )

        if request.auth_mode == "tenant":
            user_access_token = None
        elif request.auth_mode == "user" and not user_access_token:
            raise FeishuConfigError("User auth mode requires a user_access_token.")

        return (
            app_token,
            table_id,
            user_access_token,
            bool(legacy_user_access_token and user_access_token == legacy_user_access_token),
        )

    async def execute(self, request: ProgressSyncRequest) -> ProgressSyncResponse:
        trace_id = request.trace_id or str(uuid4())
        warnings: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        rows: list[dict[str, Any]] = []
        write_result = None
        records: list[ProgressRecord] = []

        try:
            adapter = self.load_adapter(request.project_id)
            routed = self.route_command(request.command_text, request.mode)
            sources = self.select_sources(adapter, routed["normalized_action"])
            records = self.read_records(adapter, sources)
            records = self.dedupe_records(records)
            records = self.apply_filters(records, routed["filters"])
            rows = self.map_rows(adapter, records)
            if request.mode == "write":
                app_token, table_id, access_token, used_legacy_user_token = self._resolve_write_auth(request)
                if not app_token or not table_id:
                    raise FeishuConfigError("Feishu Bitable target is not configured.")
                if used_legacy_user_token:
                    warnings.append(
                        {
                            "code": "LEGACY_USER_TOKEN_ENV",
                            "message": (
                                "Using a user_access_token from FEISHU_DEFAULT_BITABLE_APP_TOKEN; "
                                "move it to FEISHU_DEFAULT_USER_ACCESS_TOKEN."
                            ),
                            "retryable": False,
                            "details": {},
                        }
                    )
                token_kind = "tenant" if request.auth_mode == "tenant" else ("app" if access_token is None else "user")
                with self._internal_token_kind(token_kind):
                    schema_snapshot, schema_snapshot_created = await self.ensure_bitable_schema_snapshot(
                        table_name=self.default_bitable_table_name,
                        app_token=app_token,
                        table_id=table_id,
                        access_token=access_token,
                    )
                    if schema_snapshot is not None and schema_snapshot_created:
                        warnings.append(
                            {
                                "code": "BITABLE_SCHEMA_SNAPSHOT_READY",
                                "message": f"Bitable schema snapshot ready: {schema_snapshot.relative_to(self.repo_root)}",
                                "retryable": False,
                                "details": {},
                            }
                        )
                    write_result = await self.feishu_client.upsert_rows(
                        BitableTarget(
                            app_token=app_token,
                            table_id=table_id,
                            external_key_field=(adapter.get("field_mapping") or {}).get("external_key", "ExternalKey"),
                        ),
                        rows=rows,
                        access_token=access_token,
                    )
                    if write_result.failed:
                        warnings.extend(failure.model_dump() for failure in write_result.failures)
        except Exception as exc:
            errors.append(
                {
                    "code": exc.__class__.__name__.upper(),
                    "message": str(exc),
                    "retryable": False,
                    "details": {},
                }
            )

        summary = self.summarize(records)
        message = self._build_message(
            project_id=request.project_id,
            mode=request.mode,
            summary=summary,
            write_result=write_result,
            errors=errors,
        )

        return ProgressSyncResponse(
            ok=not errors and not bool(write_result and write_result.failed),
            mode=request.mode,
            project_id=request.project_id,
            trace_id=trace_id,
            message=message,
            summary=summary,
            records=records,
            rows=rows,
            write_result=write_result,
            warnings=warnings,
            errors=errors,
        )
