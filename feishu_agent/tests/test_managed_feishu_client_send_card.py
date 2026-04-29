"""Unit tests for ``ManagedFeishuClient.send_card`` / ``update_card`` /
``reply_card`` (T502).

We bypass the live Feishu API by stubbing the client's ``request``
method; the contract under test is narrow and mechanical:

  * correct HTTP path + receive_id_type query string
  * card dict serialised via ``json.dumps(..., ensure_ascii=False)``
    so Chinese content is preserved as UTF-8 (not ``\\uXXXX`` escapes)
  * ``send_card`` returns the server-assigned ``message_id``
  * missing ``message_id`` in response raises ``FeishuApiError``
  * empty ``chat_id`` / ``message_id`` raises ``ValueError`` before any
    network call
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from feishu_fastapi_sdk import FeishuAuthConfig
from feishu_fastapi_sdk.errors import FeishuApiError

from feishu_agent.runtime.managed_feishu_client import ManagedFeishuClient


@pytest.fixture
def client() -> ManagedFeishuClient:
    c = ManagedFeishuClient(
        FeishuAuthConfig(app_id="cli_test", app_secret="secret_test")
    )
    c.request = AsyncMock()
    return c


def _simple_card() -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {"elements": [{"tag": "markdown", "content": "你好"}]},
    }


@pytest.mark.asyncio
async def test_send_card_posts_to_im_v1_with_chat_id(client):
    client.request.return_value = {"message_id": "om_abc"}
    message_id = await client.send_card("oc_chat_1", _simple_card())
    assert message_id == "om_abc"
    client.request.assert_awaited_once()
    args, kwargs = client.request.call_args
    assert args[0] == "POST"
    assert args[1] == "/open-apis/im/v1/messages?receive_id_type=chat_id"
    body = kwargs["json_body"]
    assert body["receive_id"] == "oc_chat_1"
    assert body["msg_type"] == "interactive"
    content = json.loads(body["content"])
    assert content["schema"] == "2.0"
    assert "你好" in body["content"], "ensure_ascii=False must keep Chinese as UTF-8"


@pytest.mark.asyncio
async def test_send_card_honors_receive_id_type(client):
    client.request.return_value = {"message_id": "om_z"}
    await client.send_card("ou_user_1", _simple_card(), receive_id_type="open_id")
    args, _ = client.request.call_args
    assert args[1].endswith("receive_id_type=open_id")


@pytest.mark.asyncio
async def test_send_card_raises_when_message_id_missing(client):
    client.request.return_value = {"something_else": "x"}
    with pytest.raises(FeishuApiError, match="missing message_id"):
        await client.send_card("oc_chat_1", _simple_card())


@pytest.mark.asyncio
async def test_send_card_rejects_empty_chat_id(client):
    with pytest.raises(ValueError, match="chat_id"):
        await client.send_card("", _simple_card())
    client.request.assert_not_called()


@pytest.mark.asyncio
async def test_update_card_patches_message_endpoint(client):
    client.request.return_value = {}
    await client.update_card("om_123", _simple_card())
    args, kwargs = client.request.call_args
    assert args[0] == "PATCH"
    assert args[1] == "/open-apis/im/v1/messages/om_123"
    assert "content" in kwargs["json_body"]
    content = json.loads(kwargs["json_body"]["content"])
    assert content["schema"] == "2.0"


@pytest.mark.asyncio
async def test_update_card_rejects_empty_message_id(client):
    with pytest.raises(ValueError, match="message_id"):
        await client.update_card("", _simple_card())
    client.request.assert_not_called()


@pytest.mark.asyncio
async def test_reply_card_posts_to_reply_endpoint(client):
    client.request.return_value = {"message_id": "om_reply"}
    reply_id = await client.reply_card("om_parent", _simple_card())
    assert reply_id == "om_reply"
    args, kwargs = client.request.call_args
    assert args[0] == "POST"
    assert args[1] == "/open-apis/im/v1/messages/om_parent/reply"
    body = kwargs["json_body"]
    assert body["msg_type"] == "interactive"


@pytest.mark.asyncio
async def test_reply_card_raises_when_response_empty(client):
    client.request.return_value = {}
    with pytest.raises(FeishuApiError, match="missing message_id"):
        await client.reply_card("om_parent", _simple_card())
