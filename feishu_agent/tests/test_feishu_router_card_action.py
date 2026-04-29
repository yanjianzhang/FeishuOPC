"""Webhook-level tests for ``card.action.trigger`` dispatch (M0-C / T512).

We drive the FastAPI route via TestClient so the real
``_resolve_bot_context`` + URL-verification short-circuit + our new
``_handle_card_action_trigger`` all exercise end-to-end. The only thing
we stub out is the bot-context discovery, since the tests shouldn't
depend on repo ``.env`` being populated.

Spec references:
    specs/005-feishu-message-composer/spec.md §5bis.2 / §4.5
    specs/005-feishu-message-composer/tasks.md M0-C (T510-T513)
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from feishu_agent.routers import feishu as feishu_router_module
from feishu_agent.runtime.feishu_runtime_service import FeishuBotContext


@pytest.fixture
def app(monkeypatch) -> FastAPI:
    """Fresh FastAPI app with the feishu router mounted.

    ``_resolve_bot_context`` is monkeypatched to return a fixed fake
    context; we don't need real Feishu credentials or token verification
    for these unit tests.
    """
    fake_ctx = FeishuBotContext(
        bot_name="tech_lead",
        app_id="cli_fake",
        app_secret="secret_fake",
        verification_token=None,
        encrypt_key=None,
    )
    monkeypatch.setattr(
        feishu_router_module,
        "_resolve_bot_context",
        lambda payload: fake_ctx,
    )

    app = FastAPI()
    app.include_router(feishu_router_module.router)
    return app


def _card_action_payload(*, action_id: str = "pending_action:trace-xyz:confirm") -> dict:
    return {
        "schema": "2.0",
        "header": {
            "event_id": "evt_card_001",
            "event_type": "card.action.trigger",
            "app_id": "cli_fake",
            "tenant_key": "tenant_1",
        },
        "event": {
            "operator": {"open_id": "ou_user_1", "user_id": "u_1"},
            "token": "tok_abc",
            "action": {
                "tag": "button",
                "value": {"action_id": action_id, "diff_id": "dp_42"},
            },
            "open_message_id": "om_parent_1",
        },
    }


def _text_message_payload() -> dict:
    """A minimal im.message.receive_v1 payload used to confirm the
    dispatch branch we added does NOT affect the original path.
    """
    return {
        "schema": "2.0",
        "header": {
            "event_id": "evt_txt_001",
            "event_type": "im.message.receive_v1",
            "app_id": "cli_fake",
        },
        "event": {
            "message": {
                "chat_id": "oc_chat_1",
                "message_type": "text",
                "content": "{}",
                "message_id": "om_msg_1",
            },
        },
    }


def test_card_action_trigger_returns_toast_and_logs(app, caplog):
    client = TestClient(app)
    with caplog.at_level(logging.INFO, logger="feishu.card.action"):
        resp = client.post("/feishu/events", json=_card_action_payload())

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"toast": {"type": "info", "content": "已收到"}}

    records = [r for r in caplog.records if r.name == "feishu.card.action"]
    assert len(records) == 1, "expected exactly one structured log entry"
    rec = records[0]
    assert rec.getMessage() == "card_action_received"
    assert rec.__dict__["bot"] == "tech_lead"
    assert rec.__dict__["action_tag"] == "button"
    assert rec.__dict__["action_value"]["action_id"] == "pending_action:trace-xyz:confirm"
    assert rec.__dict__["trace"] == "evt_card_001"
    assert rec.__dict__["operator_open_id"] == "ou_user_1"
    assert rec.__dict__["open_message_id"] == "om_parent_1"


def test_card_action_trigger_does_not_invoke_process_role_message(app, monkeypatch):
    called: list[dict] = []

    async def _spy(*args, **kwargs):
        called.append(kwargs)

    monkeypatch.setattr(feishu_router_module, "process_role_message", _spy)

    client = TestClient(app)
    resp = client.post("/feishu/events", json=_card_action_payload())
    assert resp.status_code == 200
    assert called == [], (
        "card.action.trigger must not reach process_role_message in M0; "
        "that wiring lands in 005 M2 via composer.action_router"
    )


def test_unsupported_event_type_ignored_not_404(app):
    client = TestClient(app)
    resp = client.post(
        "/feishu/events",
        json={
            "header": {
                "event_id": "evt_unknown",
                "event_type": "im.chat.updated.v1",
                "app_id": "cli_fake",
            },
            "event": {},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "im.chat.updated.v1" in body["message"]


def test_url_verification_still_handled_before_event_dispatch(app):
    client = TestClient(app)
    resp = client.post(
        "/feishu/events",
        json={"type": "url_verification", "challenge": "chal_xyz", "token": "t"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"challenge": "chal_xyz"}


def test_existing_text_message_path_unchanged(app, monkeypatch):
    """Regression: the new dispatch chain must leave im.message.receive_v1
    going through the original load + process pipeline."""
    from feishu_agent.runtime.feishu_runtime_service import (
        FeishuInboundMessage,
        FeishuRuntimeResult,
    )

    async def _fake_load(**kwargs):
        return FeishuInboundMessage(message_type="text", command_text="hi", images=[])

    process_calls: list[dict] = []

    async def _fake_process(**kwargs):
        process_calls.append(kwargs)
        return FeishuRuntimeResult(
            ok=True, trace_id="t-1", message="pong", route_action=None
        )

    class _FakeFeishuClient:
        async def send_text_message(self, chat_id, text):
            self.last = (chat_id, text)

    class _FakeProgressSvc:
        def __init__(self):
            self.feishu_client = _FakeFeishuClient()

    fake_svc = _FakeProgressSvc()

    monkeypatch.setattr(feishu_router_module, "load_feishu_inbound_message", _fake_load)
    monkeypatch.setattr(feishu_router_module, "process_role_message", _fake_process)
    monkeypatch.setattr(
        feishu_router_module, "get_progress_sync_service", lambda bot_context=None: fake_svc
    )

    client = TestClient(app)
    resp = client.post("/feishu/events", json=_text_message_payload())

    assert resp.status_code == 200
    assert resp.json()["message"] == "pong"
    assert len(process_calls) == 1
    assert fake_svc.feishu_client.last == ("oc_chat_1", "pong")
