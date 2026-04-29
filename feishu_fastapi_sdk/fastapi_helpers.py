from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException, status

from feishu_fastapi_sdk.config import FeishuWebhookConfig
from feishu_fastapi_sdk.schemas.events import FeishuUrlVerificationResponse


def verify_event_token(payload: dict[str, Any], config: FeishuWebhookConfig) -> None:
    if config.verification_token and payload.get("token") != config.verification_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Feishu token.",
        )


def maybe_build_url_verification_response(payload: dict[str, Any]) -> FeishuUrlVerificationResponse | None:
    if payload.get("type") != "url_verification":
        return None
    challenge = payload.get("challenge")
    if not challenge:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Feishu url_verification event missing challenge.",
        )
    return FeishuUrlVerificationResponse(challenge=challenge)


def extract_message_text(message_content: Any) -> str:
    if isinstance(message_content, dict):
        return str(message_content.get("text") or "").strip()
    if isinstance(message_content, str):
        try:
            parsed = json.loads(message_content)
        except json.JSONDecodeError:
            return message_content.strip()
        if isinstance(parsed, dict):
            return str(parsed.get("text") or "").strip()
    return ""
