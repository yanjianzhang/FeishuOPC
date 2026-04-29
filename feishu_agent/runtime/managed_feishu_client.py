from __future__ import annotations

import json
import time
from typing import Any, Literal

import httpx
from feishu_fastapi_sdk import FeishuClient
from feishu_fastapi_sdk.errors import FeishuApiError

TOKEN_TTL_SECONDS = 6000  # Feishu tokens expire in ~7200s; refresh well before

ReceiveIdType = Literal["chat_id", "open_id", "user_id", "union_id", "email"]


class ManagedFeishuClient(FeishuClient):
    def __init__(self, *args, default_internal_token_kind: str = "app", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.default_internal_token_kind = default_internal_token_kind
        self._app_access_token: str | None = None
        self._token_acquired_at: float = 0.0

    def _tokens_expired(self) -> bool:
        return time.monotonic() - self._token_acquired_at > TOKEN_TTL_SECONDS

    async def _get_internal_tokens(self) -> tuple[str, str]:
        if self._app_access_token and self._tenant_access_token and not self._tokens_expired():
            return self._app_access_token, self._tenant_access_token

        self._ensure_config()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/open-apis/auth/v3/app_access_token/internal",
                headers={"Content-Type": "application/json; charset=utf-8"},
                json={
                    "app_id": self.auth.app_id,
                    "app_secret": self.auth.app_secret,
                },
            )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code", 0) != 0:
            raise FeishuApiError(payload.get("msg") or "Feishu app auth request failed.")

        app_access_token = payload.get("app_access_token")
        tenant_access_token = payload.get("tenant_access_token")
        if not app_access_token:
            raise FeishuApiError("Feishu app access token missing in response.")
        if not tenant_access_token:
            raise FeishuApiError("Feishu tenant access token missing in response.")

        self._app_access_token = app_access_token
        self._tenant_access_token = tenant_access_token
        self._token_acquired_at = time.monotonic()
        return app_access_token, tenant_access_token

    async def get_app_access_token(self) -> str:
        app_access_token, _ = await self._get_internal_tokens()
        return app_access_token

    async def get_tenant_access_token(self) -> str:
        _, tenant_access_token = await self._get_internal_tokens()
        return tenant_access_token

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        token = access_token
        if not token:
            if self.default_internal_token_kind == "tenant":
                token = await self.get_tenant_access_token()
            else:
                token = await self.get_app_access_token()

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

    # ------------------------------------------------------------------
    # Interactive card helpers (005 M0-A).
    #
    # These are thin wrappers over the Feishu IM v1 message APIs; they
    # expect callers to pass card dicts already conforming to the
    # Feishu Card JSON 2.0 schema (`schema: "2.0"`, `body.elements`, …).
    # Validity of the card body is the caller's responsibility — we
    # simply serialise it to the `content` field Feishu requires.
    #
    # Spec: specs/005-feishu-message-composer/spec.md §4.4 / §5bis.1.
    # ------------------------------------------------------------------

    async def send_card(
        self,
        chat_id: str,
        card: dict[str, Any],
        *,
        receive_id_type: ReceiveIdType = "chat_id",
    ) -> str:
        """Send a Feishu interactive (card) message.

        ``card`` must be a Feishu Card JSON 2.0 dict. The caller is
        responsible for constructing it (use
        ``feishu_agent.presentation.cards.v2`` helpers). We only
        serialise and send.

        Returns the Feishu-assigned ``message_id``.

        Raises :class:`FeishuApiError` on non-zero API code or missing
        ``message_id`` in the response.
        """
        if not chat_id:
            raise ValueError("send_card requires a non-empty chat_id")

        payload = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        data = await self.request(
            "POST",
            f"/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            json_body=payload,
        )
        message_id = data.get("message_id") if isinstance(data, dict) else None
        if not message_id:
            raise FeishuApiError(
                f"Feishu send_card response missing message_id (data={data!r})"
            )
        return str(message_id)

    async def update_card(
        self,
        message_id: str,
        card: dict[str, Any],
    ) -> None:
        """Update a previously-sent interactive card in place.

        Feishu enforces per-message rate limits (~60 updates/minute at
        time of writing). This method does **not** throttle; callers
        (composer delivery layer) are expected to handle 429 /
        rate-limit responses by dropping intermediate frames.
        """
        if not message_id:
            raise ValueError("update_card requires a non-empty message_id")

        payload = {"content": json.dumps(card, ensure_ascii=False)}
        await self.request(
            "PATCH",
            f"/open-apis/im/v1/messages/{message_id}",
            json_body=payload,
        )

    async def reply_card(
        self,
        message_id: str,
        card: dict[str, Any],
    ) -> str:
        """Reply to a message with an interactive card.

        Useful for card.action.trigger callbacks that want to post a
        follow-up confirmation card rather than update the original.
        Returns the newly-created reply ``message_id``.
        """
        if not message_id:
            raise ValueError("reply_card requires a non-empty message_id")

        payload = {
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        data = await self.request(
            "POST",
            f"/open-apis/im/v1/messages/{message_id}/reply",
            json_body=payload,
        )
        reply_id = data.get("message_id") if isinstance(data, dict) else None
        if not reply_id:
            raise FeishuApiError(
                f"Feishu reply_card response missing message_id (data={data!r})"
            )
        return str(reply_id)
