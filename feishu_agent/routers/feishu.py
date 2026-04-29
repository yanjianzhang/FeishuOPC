import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from feishu_fastapi_sdk import (
    FeishuApiError,
    FeishuConfigError,
    FeishuWebhookConfig,
    maybe_build_url_verification_response,
    verify_event_token,
)

from feishu_agent.auth import AuthUser, get_current_active_user
from feishu_agent.config import get_settings
from feishu_agent.runtime.feishu_runtime_service import (
    FeishuBotContext,
    available_bot_contexts,
    build_progress_sync_service,
    load_feishu_inbound_message,
    process_role_message,
)
from feishu_agent.schemas.progress_sync import ProgressSyncRequest, ProgressSyncResponse
from feishu_agent.tools.progress_sync_service import ProgressSyncService

router = APIRouter(prefix="/feishu", tags=["feishu"])
settings = get_settings()

card_action_logger = logging.getLogger("feishu.card.action")


def _resolve_bot_context(payload: dict) -> FeishuBotContext:
    app_id = ((payload.get("header") or {}).get("app_id")) or ((payload.get("event") or {}).get("app_id"))
    contexts = available_bot_contexts()
    if app_id:
        for context in contexts:
            if context.app_id == app_id:
                return context

    last_error: Exception | None = None
    for context in contexts:
        try:
            verify_event_token(
                payload,
                FeishuWebhookConfig(
                    verification_token=context.verification_token,
                    encrypt_key=context.encrypt_key,
                ),
            )
            return context
        except Exception as exc:  # verification errors are SDK-defined
            last_error = exc
            continue

    if last_error is not None:
        raise last_error
    raise HTTPException(status_code=500, detail="No Feishu bot credentials configured.")


def get_progress_sync_service(bot_context: FeishuBotContext | None = None) -> ProgressSyncService:
    return build_progress_sync_service(bot_context)


@router.post("/progress/sync", response_model=ProgressSyncResponse)
async def sync_progress(
    body: ProgressSyncRequest,
    _user: AuthUser = Depends(get_current_active_user),
):
    service = get_progress_sync_service()
    return await service.execute(body)


def _handle_card_action_trigger(
    payload: dict[str, Any],
    bot_context: FeishuBotContext,
) -> dict[str, Any]:
    """Acknowledge a Feishu card button / form callback.

    M0 scope: log the action enough to prove the webhook reaches us,
    then return the ACK-with-toast shape Feishu expects. **No business
    logic runs here** — that ships in 005 M2 via the composer's
    ``action_router``. See specs/005-feishu-message-composer/spec.md §4.5
    for the M2 hand-off contract; specs/006-feishu-input-router/spec.md
    §5.3 for the ``lark-oapi`` ``CardAction`` parsing that will feed it.

    Returns:
        Dict matching Feishu's card-callback response schema
        ({"toast": {...}}); FastAPI will serialise it.
    """
    event = payload.get("event") or {}
    header = payload.get("header") or {}
    action = event.get("action") or {}
    operator = event.get("operator") or {}

    card_action_logger.info(
        "card_action_received",
        extra={
            "trace": header.get("event_id"),
            "bot": bot_context.bot_name,
            "action_tag": action.get("tag"),
            "action_value": action.get("value"),
            "operator_open_id": operator.get("open_id"),
            "operator_user_id": operator.get("user_id"),
            "open_message_id": event.get("open_message_id") or event.get("message_id"),
        },
    )

    # TODO(005 M2 / 006 M2): parse via lark-oapi CardAction model and
    # dispatch to composer.action_router. For now, return a minimal
    # toast so the user sees that the click reached the server.
    return {"toast": {"type": "info", "content": "已收到"}}


@router.post("/events")
async def handle_events(request: Request):
    payload = await request.json()
    bot_context = _resolve_bot_context(payload)

    url_verification = maybe_build_url_verification_response(payload)
    if url_verification:
        return url_verification.model_dump()

    event = payload.get("event") or {}
    header = payload.get("header") or {}
    event_type = header.get("event_type")

    if event_type == "card.action.trigger":
        return _handle_card_action_trigger(payload, bot_context)

    if event_type != "im.message.receive_v1":
        return {
            "ok": True,
            "message": f"Ignored unsupported event_type: {event_type}",
        }

    message = event.get("message") or {}
    chat_id = message.get("chat_id")
    inbound_message = await load_feishu_inbound_message(
        bot_context=bot_context,
        message_type=message.get("message_type"),
        content=message.get("content"),
        message_id=message.get("message_id"),
    )
    if not inbound_message.has_content:
        return {"ok": True, "message": "Ignored empty or unsupported message."}

    result = await process_role_message(
        role_name=bot_context.bot_name,
        command_text=inbound_message.command_text,
        image_inputs=inbound_message.images,
        message_type=inbound_message.message_type,
        trace_id=header.get("event_id"),
        chat_id=chat_id,
        bot_context=bot_context,
    )

    if chat_id:
        try:
            service = get_progress_sync_service(bot_context)
            await service.feishu_client.send_text_message(chat_id, result.message)
        except (FeishuApiError, FeishuConfigError) as exc:
            result.warnings.append(
                {
                    "code": "FEISHU_REPLY_FAILED",
                    "message": str(exc),
                    "retryable": False,
                    "details": {"chat_id": chat_id},
                }
            )

    return {"ok": result.ok, "trace_id": result.trace_id, "message": result.message}
