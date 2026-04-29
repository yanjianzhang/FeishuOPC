"""Inbound-message parser tests.

``load_feishu_inbound_message`` is the single funnel for every Feishu
event before it reaches an agent. A regression here bricks EVERY bot
(user sees "解析消息失败，请发送文本或图片消息"), so we pin the behavior
for the three shapes Feishu actually sends in production:

- ``text`` — the common case.
- ``post`` — rich-text: any message that contains inline code, bold,
  a link, an @mention inside a formatted block, etc. Historically the
  parser treated this as empty content, which is exactly the bug that
  blocked the PM drive-by doc flow.
- ``image`` — an image-only message.

Plus a regression case for unknown types (``file`` / ``sticker`` /
future element types) — they should gracefully become empty-content
without raising, so the outer handler can reply with a friendly
"please send text or image" instead of crashing.
"""

from __future__ import annotations

import json

import pytest

from feishu_agent.runtime.feishu_runtime_service import (
    FeishuBotContext,
    load_feishu_inbound_message,
)


@pytest.fixture
def bot_context() -> FeishuBotContext:
    # The parser only dereferences ``bot_open_id`` etc. when downloading
    # images; for text/post paths it doesn't touch the context. Using
    # a permissive stub keeps the tests hermetic (no real HTTP).
    return FeishuBotContext(
        bot_name="product_manager",
        app_id="",
        app_secret="",
        verification_token="",
        encrypt_key="",
    )


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_plain_text(bot_context: FeishuBotContext):
    content = json.dumps({"text": "@产品经理 hello"})
    msg = await load_feishu_inbound_message(
        bot_context=bot_context,
        message_type="text",
        content=content,
        message_id="m1",
    )
    assert msg.message_type == "text"
    assert msg.command_text == "@产品经理 hello"
    assert msg.has_content is True


@pytest.mark.asyncio
async def test_parse_empty_text_payload(bot_context: FeishuBotContext):
    msg = await load_feishu_inbound_message(
        bot_context=bot_context,
        message_type="text",
        content=json.dumps({"text": "   "}),
        message_id="m2",
    )
    assert msg.has_content is False


# ---------------------------------------------------------------------------
# post (rich text) — THE bug fix that unblocks drive-by formatted
# messages like "...放在 `specs/backlog/review-alarm.md` ..."
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_post_with_inline_code(bot_context: FeishuBotContext):
    """Regression test for the screenshot: user @-mentions the PM bot
    and includes an inline code fragment. Previously parsed as empty,
    which produced '解析消息失败，请发送文本或图片消息'. Must now flatten
    to the concatenated plain text."""

    content = json.dumps(
        {
            "title": "",
            "content": [
                [
                    {"tag": "at", "user_id": "ou_bot", "user_name": "产品经理"},
                    {"tag": "text", "text": " 把想法写成 backlog 笔记，放在 "},
                    {"tag": "code_inline", "text": "specs/backlog/review-alarm.md"},
                    {"tag": "text", "text": "，直接推到 main 上。"},
                ]
            ],
        }
    )
    msg = await load_feishu_inbound_message(
        bot_context=bot_context,
        message_type="post",
        content=content,
        message_id="m3",
    )
    assert msg.message_type == "post"
    assert "specs/backlog/review-alarm.md" in msg.command_text
    assert "backlog 笔记" in msg.command_text
    assert "@产品经理" in msg.command_text
    assert msg.has_content is True


@pytest.mark.asyncio
async def test_parse_post_multiple_paragraphs(bot_context: FeishuBotContext):
    content = json.dumps(
        {
            "title": "PRD",
            "content": [
                [{"tag": "text", "text": "第一段"}],
                [{"tag": "text", "text": "第二段"}],
            ],
        }
    )
    msg = await load_feishu_inbound_message(
        bot_context=bot_context,
        message_type="post",
        content=content,
        message_id="m4",
    )
    assert msg.command_text == "PRD\n第一段\n第二段"


@pytest.mark.asyncio
async def test_parse_post_ignores_unknown_tags(bot_context: FeishuBotContext):
    """Future Feishu element types should not brick the parser — we
    skip unknown tags and flatten whatever text we did recognize."""

    content = json.dumps(
        {
            "content": [
                [
                    {"tag": "text", "text": "hello "},
                    {"tag": "carousel_v999", "payload": {"card_id": "x"}},
                    {"tag": "text", "text": "world"},
                ]
            ],
        }
    )
    msg = await load_feishu_inbound_message(
        bot_context=bot_context,
        message_type="post",
        content=content,
        message_id="m5",
    )
    assert msg.command_text == "hello world"


@pytest.mark.asyncio
async def test_parse_post_with_only_unknown_tags_is_empty(
    bot_context: FeishuBotContext,
):
    content = json.dumps(
        {"content": [[{"tag": "carousel_v999", "payload": {"id": "x"}}]]}
    )
    msg = await load_feishu_inbound_message(
        bot_context=bot_context,
        message_type="post",
        content=content,
        message_id="m6",
    )
    assert msg.has_content is False


@pytest.mark.asyncio
async def test_parse_post_malformed_json_returns_empty(
    bot_context: FeishuBotContext,
):
    """Never raise — the outer handler prefers a friendly reply over
    a stack trace."""

    msg = await load_feishu_inbound_message(
        bot_context=bot_context,
        message_type="post",
        content="not-valid-json{",
        message_id="m7",
    )
    assert msg.has_content is False


# ---------------------------------------------------------------------------
# unknown message types
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mtype", ["file", "sticker", "audio", ""])
async def test_parse_unknown_types_become_empty(
    bot_context: FeishuBotContext, mtype: str
):
    msg = await load_feishu_inbound_message(
        bot_context=bot_context,
        message_type=mtype,
        content="{}",
        message_id="m-unknown",
    )
    assert msg.has_content is False
