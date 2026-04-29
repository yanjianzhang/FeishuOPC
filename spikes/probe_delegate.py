"""One-shot probe for Tech Lead -> application delegate delegation.

Usage:
    # Send via Feishu IM (@mention the application agent) using bare client:
    .venv/bin/python spikes/probe_delegate.py im "读取词汇科学任务管理"

    # Exercise TechLeadToolExecutor._delegate_to_application_agent directly:
    .venv/bin/python spikes/probe_delegate.py executor "读取词汇科学任务管理"

    # Test webhook path (requires APPLICATION_AGENT_DELEGATE_URL):
    .venv/bin/python spikes/probe_delegate.py webhook "读取词汇科学任务管理"

    # Send a plain-text message WITHOUT @ mention (diagnose OpenClaw listener):
    .venv/bin/python spikes/probe_delegate.py plain "hello, 请告诉我你今天能做什么"

    # List members of APPLICATION_AGENT_GROUP_CHAT_ID (verify bots present):
    .venv/bin/python spikes/probe_delegate.py members

Pre-flight:
    - tech-lead-planner bot must be a member of APPLICATION_AGENT_GROUP_CHAT_ID
    - secrets in .larkagent/secrets/feishu_bot/*.env are loaded
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from feishu_fastapi_sdk import FeishuAuthConfig  # noqa: E402

from feishu_agent.config import get_settings  # noqa: E402
from feishu_agent.runtime.managed_feishu_client import ManagedFeishuClient  # noqa: E402
from feishu_agent.roles.tech_lead_executor import TechLeadToolExecutor  # noqa: E402


def _make_client(settings) -> ManagedFeishuClient:
    app_id = settings.tech_lead_feishu_bot_app_id or settings.feishu_bot_app_id
    app_secret = settings.tech_lead_feishu_bot_app_secret or settings.feishu_bot_app_secret
    if not app_id or not app_secret:
        raise SystemExit(
            "tech_lead_feishu_bot_app_id / app_secret missing; "
            "check .larkagent/secrets/feishu_bot/tech-lead-planner.env"
        )
    return ManagedFeishuClient(
        FeishuAuthConfig(app_id=app_id, app_secret=app_secret),
        default_internal_token_kind="tenant",
    )


async def run_im(message: str) -> None:
    settings = get_settings()
    print("[probe] APPLICATION_AGENT_OPEN_ID      =", settings.application_agent_open_id)
    print("[probe] APPLICATION_AGENT_GROUP_CHAT_ID =", settings.application_agent_group_chat_id)
    print("[probe] display_name                    =", settings.application_agent_display_name)

    if not settings.application_agent_group_chat_id:
        raise SystemExit("APPLICATION_AGENT_GROUP_CHAT_ID is not configured.")

    client = _make_client(settings)
    at = ""
    if settings.application_agent_open_id:
        at = (
            f'<at user_id="{settings.application_agent_open_id}">'
            f'{settings.application_agent_display_name}</at> '
        )
    text = f"{at}{message}"
    print("[probe] sending text =", text)

    payload = await client.request(
        "POST",
        "/open-apis/im/v1/messages?receive_id_type=chat_id",
        json_body={
            "receive_id": settings.application_agent_group_chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
    )
    print("[probe] response =", json.dumps(payload, ensure_ascii=False, indent=2))


async def run_plain(message: str) -> None:
    """Send plain text without any @mention to the app agent group."""
    settings = get_settings()
    if not settings.application_agent_group_chat_id:
        raise SystemExit("APPLICATION_AGENT_GROUP_CHAT_ID is not configured.")
    client = _make_client(settings)
    print("[probe] sending plain text (no @) =", message)
    payload = await client.request(
        "POST",
        "/open-apis/im/v1/messages?receive_id_type=chat_id",
        json_body={
            "receive_id": settings.application_agent_group_chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": message}, ensure_ascii=False),
        },
    )
    print("[probe] response =", json.dumps(payload, ensure_ascii=False, indent=2))


async def run_bot_info(env_prefix: str) -> None:
    """Fetch the bot's own open_id using the given env's AppID/Secret.

    env_prefix: one of 'application_agent' | 'tech_lead' | 'product_manager'
    Reads the secret file from .larkagent/secrets/feishu_bot/<prefix>.env
    """

    repo_root = Path(__file__).resolve().parent.parent
    candidates = {
        "application_agent": repo_root / ".larkagent/secrets/feishu_bot/application_agent.env",
        "tech_lead": repo_root / ".larkagent/secrets/feishu_bot/tech-lead-planner.env",
        "product_manager": repo_root / ".larkagent/secrets/feishu_bot/product-manager-prd.env",
    }
    path = candidates.get(env_prefix)
    if not path or not path.exists():
        raise SystemExit(f"Unknown/missing env file for prefix={env_prefix!r}")

    raw: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line and "=" not in line.split(":", 1)[0]:
            k, v = line.split(":", 1)
        elif "=" in line:
            k, v = line.split("=", 1)
        else:
            continue
        raw[k.strip()] = v.strip()

    app_id = raw.get("AppID") or raw.get("APP_ID") or ""
    app_secret = raw.get("AppSecret") or raw.get("APP_SECRET") or ""
    if not app_id or not app_secret:
        raise SystemExit(f"AppID/AppSecret missing in {path}")

    import httpx

    async with httpx.AsyncClient(timeout=10) as hc:
        tok = await hc.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/",
            json={"app_id": app_id, "app_secret": app_secret},
        )
        tok_json = tok.json()
        token = tok_json.get("tenant_access_token")
        if not token:
            raise SystemExit(f"tenant_access_token failed: {tok_json}")
        info = await hc.get(
            "https://open.feishu.cn/open-apis/bot/v3/info/",
            headers={"Authorization": f"Bearer {token}"},
        )
        info_json = info.json()

    bot = info_json.get("bot") or {}
    print(f"[probe] env={env_prefix} app_id={app_id}")
    print(f"[probe] bot name        = {bot.get('app_name')}")
    print(f"[probe] bot open_id     = {bot.get('open_id')}   <-- 写入 env 作为 *_OPEN_ID 的值")
    print(f"[probe] bot avatar_url  = {bot.get('avatar_url')}")
    print(f"[probe] bot app_status  = {bot.get('app_status')}")
    print(f"[probe] bot ip_white_list = {bot.get('ip_white_list')}")
    print(f"[probe] raw payload     = {json.dumps(info_json, ensure_ascii=False, indent=2)}")


async def run_members() -> None:
    """List members of the application agent group chat (bot + users)."""
    settings = get_settings()
    if not settings.application_agent_group_chat_id:
        raise SystemExit("APPLICATION_AGENT_GROUP_CHAT_ID is not configured.")
    client = _make_client(settings)
    chat_id = settings.application_agent_group_chat_id

    print(f"[probe] listing members of chat_id={chat_id}")
    print(f"[probe] expected APPLICATION_AGENT_OPEN_ID = {settings.application_agent_open_id}")
    print(f"[probe] expected TECH_LEAD_BOT_OPEN_ID      = {settings.tech_lead_bot_open_id}")

    members: list[dict] = []
    page_token: str | None = None
    while True:
        query = f"/open-apis/im/v1/chats/{chat_id}/members?page_size=50"
        if page_token:
            query += f"&page_token={page_token}"
        payload = await client.request("GET", query)
        data = payload.get("data") or payload
        items = data.get("items") or []
        members.extend(items)
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break

    print(f"[probe] total members = {len(members)}")
    for m in members:
        print(
            "  - name={name!r:<20} member_id={mid:<40} member_id_type={mt}".format(
                name=m.get("name"),
                mid=m.get("member_id", ""),
                mt=m.get("member_id_type", ""),
            )
        )

    expected = settings.application_agent_open_id
    hits = [m for m in members if m.get("member_id") == expected]
    if hits:
        print(f"[probe] ✅ APPLICATION_AGENT_OPEN_ID matches a member: {hits[0].get('name')}")
    else:
        print(
            "[probe] ⚠️  APPLICATION_AGENT_OPEN_ID is NOT in the group's member list.\n"
            "        → the @ in text is only a visual tag; the delegate bot may not actually be a chat member,\n"
            "          so Feishu will not deliver the event to OpenClaw."
        )


async def run_executor(message: str, force_webhook: bool) -> None:
    """Invoke TechLeadToolExecutor._delegate_to_application_agent directly.

    Uses ``object.__new__`` to bypass __init__ so we don't have to supply
    every dependency; only the attributes touched by the delegate tool
    are populated.
    """

    settings = get_settings()

    executor = object.__new__(TechLeadToolExecutor)
    executor._feishu_client = _make_client(settings)
    executor._app_agent_open_id = settings.application_agent_open_id
    executor._app_agent_group_chat_id = settings.application_agent_group_chat_id
    executor._app_agent_label = settings.application_agent_display_name or "Application delegate"
    executor._app_delegate_url = (
        settings.application_agent_delegate_url if force_webhook else None
    )
    executor._tech_lead_bot_open_id = settings.tech_lead_bot_open_id
    executor.project_id = "probe-project"
    executor.trace_id = "probe-trace"
    executor.chat_id = settings.application_agent_group_chat_id
    executor.role_name = "tech_lead"

    args = SimpleNamespace(message=message)
    result = await TechLeadToolExecutor._delegate_to_application_agent(executor, args)
    print("[probe] result =", json.dumps(result, ensure_ascii=False, indent=2))


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    mode = sys.argv[1]
    if mode == "members":
        asyncio.run(run_members())
        return
    if mode == "bot-info":
        prefix = sys.argv[2] if len(sys.argv) >= 3 else "application_agent"
        asyncio.run(run_bot_info(prefix))
        return
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    message = sys.argv[2]

    if mode == "im":
        asyncio.run(run_im(message))
    elif mode == "executor":
        asyncio.run(run_executor(message, force_webhook=False))
    elif mode == "webhook":
        asyncio.run(run_executor(message, force_webhook=True))
    elif mode == "plain":
        asyncio.run(run_plain(message))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
