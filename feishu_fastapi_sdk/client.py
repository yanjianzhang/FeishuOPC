from __future__ import annotations

import json
from typing import Any

import httpx

from feishu_fastapi_sdk.config import BitableTarget, FeishuAuthConfig
from feishu_fastapi_sdk.errors import FeishuApiError, FeishuConfigError
from feishu_fastapi_sdk.schemas.bitable import BitableWriteFailure, BitableWriteResult


class FeishuClient:
    def __init__(
        self,
        auth: FeishuAuthConfig,
        *,
        base_url: str = "https://open.feishu.cn",
        timeout: float = 15.0,
    ) -> None:
        self.auth = auth
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._tenant_access_token: str | None = None

    def _ensure_config(self) -> None:
        if not self.auth.app_id or not self.auth.app_secret:
            raise FeishuConfigError("Feishu app credentials are not configured.")

    @staticmethod
    def _unwrap_data(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("code", 0) != 0:
            raise FeishuApiError(payload.get("msg") or "Feishu API request failed.")
        return payload.get("data") or {}

    async def get_tenant_access_token(self) -> str:
        if self._tenant_access_token:
            return self._tenant_access_token

        self._ensure_config()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal",
                json={
                    "app_id": self.auth.app_id,
                    "app_secret": self.auth.app_secret,
                },
            )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code", 0) != 0:
            raise FeishuApiError(payload.get("msg") or "Feishu auth request failed.")
        token = payload.get("tenant_access_token")
        if not token:
            raise FeishuApiError("Feishu tenant access token missing in response.")
        self._tenant_access_token = token
        return token

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        token = access_token or await self.get_tenant_access_token()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(
                method,
                f"{self.base_url}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json=json_body,
            )
        response.raise_for_status()
        return self._unwrap_data(response.json())

    async def send_text_message(
        self,
        chat_id: str,
        text: str,
        *,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/open-apis/im/v1/messages?receive_id_type=chat_id",
            json_body={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
            access_token=access_token,
        )

    async def search_record_by_field(
        self,
        target: BitableTarget,
        field_name: str,
        value: str,
        *,
        access_token: str | None = None,
    ) -> str | None:
        data = await self.request(
            "POST",
            f"/open-apis/bitable/v1/apps/{target.app_token}/tables/{target.table_id}/records/search",
            json_body={
                "automatic_fields": False,
                "field_names": [field_name],
                "filter": {
                    "conjunction": "and",
                    "conditions": [
                        {
                            "field_name": field_name,
                            "operator": "is",
                            "value": [value],
                        }
                    ],
                },
                "page_size": 1,
            },
            access_token=access_token,
        )
        items = data.get("items") or []
        if not items:
            return None
        return items[0].get("record_id")

    async def create_record(
        self,
        target: BitableTarget,
        fields: dict[str, Any],
        *,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            f"/open-apis/bitable/v1/apps/{target.app_token}/tables/{target.table_id}/records",
            json_body={"fields": fields},
            access_token=access_token,
        )

    async def update_record(
        self,
        target: BitableTarget,
        record_id: str,
        fields: dict[str, Any],
        *,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        return await self.request(
            "PUT",
            f"/open-apis/bitable/v1/apps/{target.app_token}/tables/{target.table_id}/records/{record_id}",
            json_body={"fields": fields},
            access_token=access_token,
        )

    async def upsert_rows(
        self,
        target: BitableTarget,
        rows: list[dict[str, Any]],
        *,
        access_token: str | None = None,
    ) -> BitableWriteResult:
        result = BitableWriteResult()
        for row in rows:
            external_key = row.get(target.external_key_field)
            if not external_key:
                result.failed += 1
                result.failures.append(
                    BitableWriteFailure(
                        code="EXTERNAL_KEY_MISSING",
                        message="Row missing external key.",
                        row=row,
                    )
                )
                continue

            try:
                record_id = await self.search_record_by_field(
                    target,
                    target.external_key_field,
                    str(external_key),
                    access_token=access_token,
                )
                if record_id:
                    await self.update_record(target, record_id, row, access_token=access_token)
                    result.updated += 1
                else:
                    await self.create_record(target, row, access_token=access_token)
                    result.created += 1
            except Exception as exc:  # pragma: no cover
                result.failed += 1
                result.failures.append(
                    BitableWriteFailure(
                        code="BITABLE_UPSERT_FAILED",
                        message=str(exc),
                        external_key=str(external_key),
                    )
                )
        return result
