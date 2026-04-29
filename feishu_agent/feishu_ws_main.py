from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    DeleteMessageReactionRequest,
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from lark_oapi.api.im.v1.model.emoji import Emoji

from feishu_agent.config import get_settings
from feishu_agent.runtime.feishu_runtime_service import (
    FeishuThreadContext,
    load_feishu_inbound_message,
    process_role_message,
    resolve_bot_context_for_role,
)
from feishu_agent.runtime.message_deduper import MessageDeduper
from feishu_agent.team.pending_action_service import PendingActionService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("feishu-ws")
settings = get_settings()

ROLE_NAME = os.environ.get("LARK_ROLE_NAME", "tech-lead-planner")
# Outer per-Feishu-message timeout. MUST be >= the inner role-agent
# budget, otherwise the outer wrapper fires first and reports "失败"
# to the user while the inner coroutine keeps burning LLM budget
# (see 2026-04-20 Story 3-2 incident: outer 600s timed out at
# 00:52 but TL kept dispatching reviewer 2 until 00:57).
# Default = role_agent_timeout_seconds + 60s overhead buffer. Callers
# can still override via env if they really want a shorter envelope.
MESSAGE_TIMEOUT_SECONDS = float(
    os.environ.get(
        "LARK_MESSAGE_TIMEOUT_SECONDS",
        str(int(settings.role_agent_timeout_seconds) + 60),
    )
)
EVENT_DEDUP_TTL_SECONDS = float(os.environ.get("LARK_EVENT_DEDUP_TTL_SECONDS", "3600"))
# Wall-clock age cutoff for the "pending bypass" (see
# ``_chat_has_pending_for_this_role``). Older pending files still
# execute correctly when explicitly @-mentioned — they just stop
# letting unmentioned group messages slip past the mention filter.
# Defaults to 24 hours: long enough for a user to sleep on a
# confirmation prompt, short enough that a forgotten pending doesn't
# quietly reopen the bot to arbitrary group traffic.
PENDING_BYPASS_TTL_SECONDS = float(
    os.environ.get("LARK_PENDING_BYPASS_TTL_SECONDS", "86400")
)
BOT_CONTEXT = resolve_bot_context_for_role(ROLE_NAME)
CLIENT = lark.Client.builder().app_id(BOT_CONTEXT.app_id).app_secret(BOT_CONTEXT.app_secret).build()


def _fetch_bot_open_id() -> str | None:
    """Fetch this bot's open_id via /bot/v3/info.

    Called in a background thread (see ``_start_bot_open_id_fetch``) —
    this used to run synchronously at module import time and could
    block Python startup for up to 20s if Feishu's edge was slow. That
    in turn made ``systemctl start`` look hung and tripped deploy
    healthchecks. The background-thread version returns immediately;
    callers that happen to race the fetch just skip mention-filtering
    for a few messages (``_bot_is_mentioned`` returns True when the
    open_id is missing), which is strictly safer than blocking import.
    """
    import httpx
    try:
        resp = httpx.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/",
            json={"app_id": BOT_CONTEXT.app_id, "app_secret": BOT_CONTEXT.app_secret},
            timeout=10,
        )
        token = resp.json().get("tenant_access_token", "")
        if not token:
            return None
        info_resp = httpx.get(
            "https://open.feishu.cn/open-apis/bot/v3/info/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        return info_resp.json().get("bot", {}).get("open_id")
    except Exception:
        logger.warning("Failed to fetch bot open_id, group mention filtering disabled", exc_info=True)
        return None


def _start_bot_open_id_fetch() -> None:
    """Kick off ``_fetch_bot_open_id`` on a daemon thread so import /
    process start is never gated on Feishu reachability."""

    def _runner() -> None:
        nonlocal_open_id = _fetch_bot_open_id()
        if not nonlocal_open_id:
            logger.warning(
                "Could not resolve bot open_id for role=%s, "
                "group mention filtering disabled",
                ROLE_NAME,
            )
            return
        from dataclasses import replace

        global BOT_CONTEXT
        BOT_CONTEXT = replace(BOT_CONTEXT, bot_open_id=nonlocal_open_id)
        logger.info(
            "Bot open_id resolved: %s for role=%s", nonlocal_open_id, ROLE_NAME
        )

    threading.Thread(
        target=_runner,
        name="feishu-ws-open-id-fetch",
        daemon=True,
    ).start()


_start_bot_open_id_fetch()
ASYNC_LOOP = asyncio.new_event_loop()
MESSAGE_DEDUPER = MessageDeduper(ttl_seconds=EVENT_DEDUP_TTL_SECONDS)


def _run_async_loop_forever(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


ASYNC_LOOP_THREAD = threading.Thread(
    target=_run_async_loop_forever,
    args=(ASYNC_LOOP,),
    name="feishu-ws-async-loop",
    daemon=True,
)
ASYNC_LOOP_THREAD.start()


def _conversation_log_path() -> Path:
    repo_root = Path(settings.app_repo_root or os.environ.get("APP_REPO_ROOT") or Path.cwd())
    log_dir = repo_root / settings.techbot_run_log_dir / "conversations"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{ROLE_NAME}.jsonl"


def _append_conversation_log(
    *,
    trace_id: str | None,
    chat_id: str | None,
    message_id: str | None,
    user_text: str,
    reply_text: str,
    route_action: str | None,
    target_table_name: str | None,
    send_ok: bool,
) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "role_name": ROLE_NAME,
        "trace_id": trace_id,
        "chat_id": chat_id,
        "message_id": message_id,
        "user_text": user_text,
        "reply_text": reply_text,
        "route_action": route_action,
        "target_table_name": target_table_name,
        "send_ok": send_ok,
    }
    with _conversation_log_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _send_reply(message, content_text: str, *, reply_to_message_id: str | None = None) -> None:
    content = json.dumps({"text": content_text}, ensure_ascii=False)

    if message.chat_type == "p2p" and not reply_to_message_id:
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(message.chat_id)
                .msg_type("text")
                .content(content)
                .build()
            )
            .build()
        )
        response = CLIENT.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError(
                f"client.im.v1.message.create failed, code: {response.code}, msg: {response.msg}, log_id: {response.get_log_id()}"
            )
        return

    target_id = reply_to_message_id or message.message_id
    request = (
        ReplyMessageRequest.builder()
        .message_id(target_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .content(content)
            .msg_type("text")
            .build()
        )
        .build()
    )
    response = CLIENT.im.v1.message.reply(request)
    if not response.success():
        raise RuntimeError(
            f"client.im.v1.message.reply failed, code: {response.code}, msg: {response.msg}, log_id: {response.get_log_id()}"
        )


def _trace_id_from_event(data: P2ImMessageReceiveV1) -> str | None:
    header = getattr(data, "header", None)
    return getattr(header, "event_id", None)


def _event_key(trace_id: str | None, message_id: str | None) -> str | None:
    if trace_id and message_id:
        return f"{trace_id}:{message_id}"
    return trace_id or message_id


def _format_processing_error(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return f"TimeoutError: 处理超时，超过 {int(MESSAGE_TIMEOUT_SECONDS)} 秒仍未完成。"
    details = str(exc).strip()
    if details:
        return f"{exc.__class__.__name__}: {details}"
    return exc.__class__.__name__


def _load_inbound_message(message):
    future = asyncio.run_coroutine_threadsafe(
        load_feishu_inbound_message(
            bot_context=BOT_CONTEXT,
            message_type=message.message_type,
            content=message.content,
            message_id=getattr(message, "message_id", None),
        ),
        ASYNC_LOOP,
    )
    try:
        return future.result(timeout=MESSAGE_TIMEOUT_SECONDS)
    except (concurrent.futures.TimeoutError, TimeoutError):
        # Cancel the inner coroutine so it doesn't keep running in
        # the background after we've already reported the timeout
        # to the Feishu client. Without this, the task leaks and
        # continues dispatching sub-agents, confusing the user
        # ("处理超时" in chat but sub-agent completions still
        # stream in afterwards).
        future.cancel()
        raise


def _send_topic_message(text: str, *, reply_to_message_id: str) -> None:
    """Send a best-effort reply into a topic thread. Logs on failure instead of raising."""
    try:
        content = json.dumps({"text": text}, ensure_ascii=False)
        request = (
            ReplyMessageRequest.builder()
            .message_id(reply_to_message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .content(content)
                .msg_type("text")
                .build()
            )
            .build()
        )
        response = CLIENT.im.v1.message.reply(request)
        if not response.success():
            logger.warning(
                "topic message reply failed, code=%s msg=%s log_id=%s",
                response.code, response.msg, response.get_log_id(),
            )
    except Exception:
        logger.warning("topic message send error", exc_info=True)


def _add_reaction(message_id: str, emoji_type: str = "OnIt") -> str | None:
    """Add an emoji reaction to a message. Returns reaction_id on success."""
    try:
        request = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                .build()
            )
            .build()
        )
        response = CLIENT.im.v1.message_reaction.create(request)
        if response.success() and response.data:
            return response.data.reaction_id
        logger.warning(
            "add reaction failed, code=%s msg=%s", response.code, response.msg,
        )
    except Exception:
        logger.warning("add reaction error", exc_info=True)
    return None


def _remove_reaction(message_id: str, reaction_id: str) -> None:
    """Remove a previously added emoji reaction."""
    try:
        request = (
            DeleteMessageReactionRequest.builder()
            .message_id(message_id)
            .reaction_id(reaction_id)
            .build()
        )
        response = CLIENT.im.v1.message_reaction.delete(request)
        if not response.success():
            logger.warning(
                "remove reaction failed, code=%s msg=%s", response.code, response.msg,
            )
    except Exception:
        logger.warning("remove reaction error", exc_info=True)


def _build_thread_context(message) -> FeishuThreadContext:
    return FeishuThreadContext(
        chat_id=message.chat_id,
        message_id=message.message_id,
        root_id=getattr(message, "root_id", None) or None,
        thread_id=getattr(message, "thread_id", None) or None,
        chat_type=getattr(message, "chat_type", "p2p"),
    )


def _run_role_message(inbound_message, trace_id: str | None, chat_id: str | None, thread_ctx: FeishuThreadContext | None = None):
    thread_update_fn = None
    if thread_ctx and thread_ctx.is_topic:
        target_id = thread_ctx.reply_target_id

        def thread_update_fn(text: str) -> None:
            _send_topic_message(text, reply_to_message_id=target_id)

    future = asyncio.run_coroutine_threadsafe(
        process_role_message(
            role_name=ROLE_NAME,
            command_text=inbound_message.command_text,
            image_inputs=inbound_message.images,
            message_type=inbound_message.message_type,
            trace_id=trace_id,
            chat_id=chat_id,
            bot_context=BOT_CONTEXT,
            thread_context=thread_ctx,
            thread_update_fn=thread_update_fn,
        ),
        ASYNC_LOOP,
    )
    try:
        return future.result(timeout=MESSAGE_TIMEOUT_SECONDS)
    except (concurrent.futures.TimeoutError, TimeoutError):
        # Cancel the inner role-agent coroutine so it doesn't keep
        # dispatching sub-agents after we've already reported
        # "处理超时" back to the user (see 2026-04-20 Story 3-2
        # incident: outer 600s fired at 00:52 but TL leaked past
        # that point, kept running until 00:57, and dispatched a
        # reviewer 2 that inherited a 40s budget and failed).
        future.cancel()
        raise


def _chat_has_pending_for_this_role(chat_id: str | None) -> bool:
    """Return True if this bot has a *fresh* PendingAction waiting on this chat.

    Covers **every** confirmation/wait-for-reply flow that persists a
    PendingAction via ``PendingActionService`` — today that includes
    ``write_progress_sync`` (TechLead/PM progress sync), ``force_sync_to_remote``
    (preflight divergence recovery), plus any future ``action_type`` routed
    through ``request_confirmation`` or the preflight enqueuer. Any file
    under ``pending/`` matching this chat_id and younger than
    ``PENDING_BYPASS_TTL_SECONDS`` is treated as proof that this bot is
    actively waiting for the user's next reply.

    **TTL guard (H-1):** pending files are only deleted on explicit
    confirm/cancel. Without a TTL, a forgotten prompt from last week
    would keep the @mention filter open indefinitely — effectively
    letting arbitrary unmentioned group chatter reach the bot. The
    TTL bounds that exposure to roughly one user-response window.

    **role_name matching (M-1):**
    ``pending.role_name`` stores the **internal short bot name**
    (``"tech_lead"`` / ``"product_manager"`` — written by
    ``TechLeadToolExecutor`` and ``_enqueue_force_sync_pending``), **not**
    the deployment role slug (``tech-lead-planner`` etc.). Matching uses
    ``BOT_CONTEXT.bot_name`` which is already normalized via
    ``resolve_bot_context_for_role``. An empty ``pending.role_name``
    (legacy/pre-role format) is only accepted for the canonical bots
    (``tech_lead`` / ``product_manager``) — never for the ``default``
    fallback context, because in a multi-bot deployment the default
    context has no way to disambiguate whose pending it is.
    """
    if not chat_id:
        return False
    repo_root = Path(settings.app_repo_root or os.environ.get("APP_REPO_ROOT") or Path.cwd())
    pending_dir = repo_root / settings.techbot_run_log_dir / "pending"
    try:
        service = PendingActionService(pending_dir)
        pending = service.load_by_chat_id(chat_id)
    except Exception:
        logger.debug("pending lookup failed for chat_id=%s", chat_id, exc_info=True)
        return False
    if pending is None:
        return False

    expected_bot = (BOT_CONTEXT.bot_name or "").strip()
    pending_role = (pending.role_name or "").strip()
    if pending_role:
        if not expected_bot or pending_role != expected_bot:
            return False
    else:
        # Legacy pending without a role_name. Only canonical bots may
        # assume it's theirs; the ``default`` catch-all bot must not
        # blanket-claim unattributed pending files.
        if expected_bot not in {"tech_lead", "product_manager"}:
            return False

    # TTL — drop pending files older than the configured window from
    # the bypass decision. They stay on disk so a later explicit @
    # mention can still resume/cancel them; we only refuse to wave
    # unmentioned messages through on their behalf.
    pending_path = pending_dir / f"{pending.trace_id}.json"
    try:
        age_seconds = time.time() - pending_path.stat().st_mtime
    except OSError:
        # File vanished between load and stat; treat as stale.
        return False
    if age_seconds > PENDING_BYPASS_TTL_SECONDS:
        logger.info(
            "pending action is older than TTL (%.0fs > %.0fs); not bypassing mention filter for chat_id=%s trace_id=%s",
            age_seconds,
            PENDING_BYPASS_TTL_SECONDS,
            chat_id,
            pending.trace_id,
        )
        return False
    return True


def _bot_is_mentioned(message) -> bool:
    """Return True if this bot's open_id appears in the message mentions."""
    if not BOT_CONTEXT.bot_open_id:
        return True  # can't filter without open_id, allow all
    mentions = getattr(message, "mentions", None)
    if not mentions:
        return False
    for m in mentions:
        mention_id = getattr(m, "id", None) if not isinstance(m, dict) else m.get("id")
        if not mention_id:
            continue
        if isinstance(mention_id, str):
            mid = mention_id
        else:
            mid = getattr(mention_id, "open_id", None) or getattr(mention_id, "union_id", None) or ""
        if mid == BOT_CONTEXT.bot_open_id:
            return True
    return False


def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
    message = data.event.message
    trace_id = _trace_id_from_event(data)
    event_key = _event_key(trace_id, getattr(message, "message_id", None))
    send_ok = False
    route_action = None
    target_table_name = None
    message_text = ""

    chat_type = getattr(message, "chat_type", "p2p")
    chat_id_value = getattr(message, "chat_id", None)
    if chat_type == "group" and not _bot_is_mentioned(message):
        # Pending-bypass: whenever this bot is awaiting a user reply
        # (any action_type persisted via PendingActionService —
        # confirmations from request_confirmation, force_sync_to_remote,
        # etc.), accept the next unmentioned message in that chat. The
        # pending file is the single source of truth for "this bot is
        # engaged in this conversation". Covers the "话题里只有我和他"
        # case where the user naturally replies "确认" without @mention.
        if _chat_has_pending_for_this_role(chat_id_value):
            logger.info(
                "Accepting unmentioned group message because of pending action for role=%s bot=%s chat_id=%s trace_id=%s",
                ROLE_NAME, BOT_CONTEXT.bot_name, chat_id_value, trace_id,
            )
        else:
            logger.info(
                "Ignored group message not mentioning this bot for role=%s trace_id=%s",
                ROLE_NAME, trace_id,
            )
            return

    if not MESSAGE_DEDUPER.should_process(event_key):
        logger.info(
            "Ignored duplicate Feishu event for role=%s trace_id=%s message_id=%s",
            ROLE_NAME,
            trace_id,
            getattr(message, "message_id", None),
        )
        return

    thread_ctx = _build_thread_context(message)
    ack_reaction_id = _add_reaction(message.message_id, "OnIt")

    try:
        inbound_message = _load_inbound_message(message)
        message_text = inbound_message.command_text
        if not inbound_message.has_content:
            reply_text = "解析消息失败，请发送文本或图片消息"
        else:
            try:
                logger.info(
                    "Received %s message for role=%s trace_id=%s chat_id=%s text=%r image_count=%d thread_id=%s root_id=%s",
                    inbound_message.message_type,
                    ROLE_NAME,
                    trace_id,
                    message.chat_id,
                    message_text,
                    len(inbound_message.images),
                    thread_ctx.thread_id,
                    thread_ctx.root_id,
                )
                result = _run_role_message(inbound_message, trace_id, message.chat_id, thread_ctx)
                reply_text = result.message
                route_action = result.route_action
                target_table_name = result.target_table_name
                logger.info(
                    "Processed %s message for role=%s trace_id=%s route_action=%s target_table=%s ok=%s warnings=%d",
                    inbound_message.message_type,
                    ROLE_NAME,
                    result.trace_id,
                    route_action,
                    target_table_name,
                    result.ok,
                    len(result.warnings),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to process incoming message for role %s", ROLE_NAME)
                reply_text = f"{ROLE_NAME} 处理消息失败：{_format_processing_error(exc)}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to parse incoming message for role %s", ROLE_NAME)
        reply_text = f"{ROLE_NAME} 解析消息失败：{_format_processing_error(exc)}"

    reply_target_id = thread_ctx.reply_target_id if thread_ctx.is_topic else None
    try:
        _send_reply(message, reply_text, reply_to_message_id=reply_target_id)
        send_ok = True
    except Exception:  # noqa: BLE001
        logger.exception("Failed to reply to message for role %s", ROLE_NAME)
    finally:
        if ack_reaction_id:
            _remove_reaction(message.message_id, ack_reaction_id)
        try:
            MESSAGE_DEDUPER.mark_finished(event_key, keep=send_ok)
            _append_conversation_log(
                trace_id=trace_id,
                chat_id=message.chat_id,
                message_id=message.message_id,
                user_text=message_text,
                reply_text=reply_text,
                route_action=route_action,
                target_table_name=target_table_name,
                send_ok=send_ok,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to append conversation log for role %s", ROLE_NAME)


EVENT_HANDLER = (
    lark.EventDispatcherHandler.builder(
        BOT_CONTEXT.verification_token or "",
        BOT_CONTEXT.encrypt_key or "",
    )
    .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
    .build()
)

WS_CLIENT = lark.ws.Client(
    BOT_CONTEXT.app_id,
    BOT_CONTEXT.app_secret,
    event_handler=EVENT_HANDLER,
    log_level=lark.LogLevel.DEBUG,
)


def main() -> None:
    logger.info(
        "Starting Feishu WS client for role=%s bot=%s message_timeout_seconds=%s",
        ROLE_NAME,
        BOT_CONTEXT.bot_name,
        MESSAGE_TIMEOUT_SECONDS,
    )
    WS_CLIENT.start()


if __name__ == "__main__":
    main()
