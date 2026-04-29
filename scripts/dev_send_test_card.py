#!/usr/bin/env python3
"""Dev smoke script for the 005 M0-A card path (T517).

Exercises every FeishuClient card method we landed in M0-A/B against a
real Feishu tenant, using only JSON 2.0 cards built via the
``feishu_agent.presentation.cards.v2`` helpers. This is the closing
gate for M0 (spec 005 §12 M0): if this script sends a card that
renders, round-trips an update, and fires a ``card.action.trigger``
callback that our new router handler logs, M0 is done.

Usage
-----
Pick a bot and a chat_id you have write access to, then:

    # baseline: send a two-button card
    python scripts/dev_send_test_card.py --bot tech_lead --chat-id oc_xxx

    # update-in-place: send then patch the header
    python scripts/dev_send_test_card.py --bot tech_lead --chat-id oc_xxx --update

    # streaming: send with streaming_mode=True, then switch it off
    python scripts/dev_send_test_card.py --bot tech_lead --chat-id oc_xxx --streaming

    # reply: send, then reply-thread a second card
    python scripts/dev_send_test_card.py --bot tech_lead --chat-id oc_xxx --reply

Expected manual verification per T520 lives in
``specs/005-feishu-message-composer/tasks.md`` M0-E.

Exit codes
----------
    0  success
    1  FeishuApiError (network / auth / quota / malformed card at server)
    2  CLI or config error (bad --bot, no credentials, malformed local card)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from feishu_fastapi_sdk import FeishuAuthConfig
from feishu_fastapi_sdk.errors import FeishuApiError

from feishu_agent.presentation.cards import v2
from feishu_agent.runtime.feishu_runtime_service import (
    FeishuBotContext,
    available_bot_contexts,
)
from feishu_agent.runtime.managed_feishu_client import ManagedFeishuClient
from feishu_agent.tests._v2_card_assertions import assert_valid_v2_card

EXIT_OK = 0
EXIT_FEISHU_ERROR = 1
EXIT_CLI_ERROR = 2


def _build_baseline_card(*, streaming: bool = False) -> dict[str, Any]:
    """Two-button pending-action-ish card; streaming toggles 2.0 mode."""
    return v2.card(
        header=v2.header(
            title=("执行中…" if streaming else "Smoke card"),
            subtitle="005 M0 smoke",
            template=("yellow" if streaming else "blue"),
        ),
        body=v2.body(elements=[
            v2.div_markdown(
                "**测试文本** — 点击按钮应在 agent server 看到 "
                "`feishu.card.action` 结构化日志。"
            ),
            v2.hr(),
            v2.action_row(actions=[
                v2.button(
                    text="主要操作",
                    action_id="smoke:dev:confirm",
                    style="primary",
                    value={"name": "confirm"},
                ),
                v2.button(
                    text="次要操作",
                    action_id="smoke:dev:cancel",
                    value={"name": "cancel"},
                ),
            ]),
        ]),
        streaming_mode=streaming,
        summary=("生成中…" if streaming else None),
    )


def _build_updated_card() -> dict[str, Any]:
    return v2.card(
        header=v2.header(title="已更新", subtitle="update_card 通道", template="green"),
        body=v2.body(elements=[
            v2.div_markdown("**更新成功** — 原卡片已被 `update_card` 改写。"),
        ]),
    )


def _build_streaming_final_card() -> dict[str, Any]:
    return v2.card(
        header=v2.header(title="执行完成", subtitle="streaming 结束帧", template="green"),
        body=v2.body(elements=[
            v2.div_markdown("**结束** — streaming_mode=False，summary 清除。"),
            v2.collapsible(
                title="查看明细 (3 步)",
                elements=[
                    v2.div_plain_text("• step 1 — done (120ms)"),
                    v2.div_plain_text("• step 2 — done (45ms)"),
                    v2.div_plain_text("• step 3 — done (310ms)"),
                ],
            ),
        ]),
    )


def _build_reply_card() -> dict[str, Any]:
    return v2.card(
        header=v2.header(title="Reply", subtitle="reply_card 通道", template="wathet"),
        body=v2.body(elements=[
            v2.div_markdown("**回帖** — `reply_card` 产出的子消息。"),
        ]),
    )


def _pick_bot(name: str) -> FeishuBotContext:
    ctxs = available_bot_contexts()
    if not ctxs:
        raise SystemExit(
            "No Feishu bot credentials in .env — configure tech-lead / "
            "product-manager / default before running smoke."
        )
    matching = [c for c in ctxs if c.bot_name == name]
    if not matching:
        raise SystemExit(
            f"Bot {name!r} not configured. Available: "
            + ", ".join(c.bot_name for c in ctxs)
        )
    return matching[0]


def _build_client(ctx: FeishuBotContext) -> ManagedFeishuClient:
    return ManagedFeishuClient(
        FeishuAuthConfig(app_id=ctx.app_id, app_secret=ctx.app_secret),
        default_internal_token_kind="tenant",
    )


async def _run_baseline(client: ManagedFeishuClient, chat_id: str) -> str:
    card = _build_baseline_card()
    assert_valid_v2_card(card)
    print(f"[smoke] send_card → chat={chat_id}")
    message_id = await client.send_card(chat_id, card)
    print(f"[smoke] → message_id={message_id}")
    return message_id


async def _run_update(client: ManagedFeishuClient, chat_id: str) -> str:
    message_id = await _run_baseline(client, chat_id)
    print("[smoke] sleeping 3s before update_card …")
    await asyncio.sleep(3)
    card = _build_updated_card()
    assert_valid_v2_card(card)
    await client.update_card(message_id, card)
    print("[smoke] update_card OK")
    return message_id


async def _run_streaming(client: ManagedFeishuClient, chat_id: str) -> str:
    card = _build_baseline_card(streaming=True)
    assert_valid_v2_card(card)
    print(f"[smoke] send_card (streaming) → chat={chat_id}")
    message_id = await client.send_card(chat_id, card)
    print(f"[smoke] → message_id={message_id} (summary='生成中…')")
    print("[smoke] sleeping 3s before streaming-off update_card …")
    await asyncio.sleep(3)
    final_card = _build_streaming_final_card()
    assert_valid_v2_card(final_card)
    await client.update_card(message_id, final_card)
    print("[smoke] update_card OK (streaming_mode=False)")
    return message_id


async def _run_reply(client: ManagedFeishuClient, chat_id: str) -> str:
    message_id = await _run_baseline(client, chat_id)
    print("[smoke] reply_card …")
    card = _build_reply_card()
    assert_valid_v2_card(card)
    reply_id = await client.reply_card(message_id, card)
    print(f"[smoke] → reply message_id={reply_id}")
    return reply_id


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="dev_send_test_card",
        description="Feishu Card JSON 2.0 smoke test (005 M0).",
    )
    p.add_argument("--bot", required=True, help="bot context name (tech_lead / product_manager / default)")
    p.add_argument("--chat-id", required=True, help="target chat_id (oc_*)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--update", action="store_true", help="send then update_card the header")
    mode.add_argument("--streaming", action="store_true", help="send with streaming_mode=True, then switch off")
    mode.add_argument("--reply", action="store_true", help="send then reply_card against it")
    return p.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:
    ctx = _pick_bot(args.bot)
    client = _build_client(ctx)

    try:
        if args.update:
            await _run_update(client, args.chat_id)
        elif args.streaming:
            await _run_streaming(client, args.chat_id)
        elif args.reply:
            await _run_reply(client, args.chat_id)
        else:
            await _run_baseline(client, args.chat_id)
    except FeishuApiError as exc:
        print(f"[smoke] FeishuApiError: {exc}", file=sys.stderr)
        return EXIT_FEISHU_ERROR
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_CLI_ERROR
    try:
        return asyncio.run(_amain(args))
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CLI_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
