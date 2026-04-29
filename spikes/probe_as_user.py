"""Send Feishu IM messages as a real human user (user_access_token path).

Use this to bypass the "bot cannot @ another bot" limitation: with a
user_access_token, the message is attributed to the authorizing human
(e.g. a teammate), and OpenClaw-hosted delegate bots will treat the
mention as coming from a real user and respond normally.

Modes:
    auth-server   Start a localhost callback server, print the authorize
                  URL, wait for you to grant consent in the browser, then
                  exchange the code for tokens and persist to disk.
    auth-url      Print only the authorize URL (manual paste flow).
    auth-exchange <code>
                  Manually exchange an authorization code for tokens.
    auth-status   Show locally stored token metadata (expiry etc.).
    as-user "<msg>"
                  Send a Feishu text message as the authorized user,
                  with an @mention of APPLICATION_AGENT_OPEN_ID prepended
                  to the text. Refreshes the access_token automatically.

All modes accept --app=<prefix> where prefix is one of
    application_agent | tech_lead | product_manager
and defaults to application_agent (its AppSecret has been verified).

Pre-flight in Feishu 开放平台:
    1. For the chosen app, 权限管理 -> 添加 im:message 权限
    2. 应用安全 -> 重定向 URL 白名单加 http://localhost:18765/feishu/callback
    3. 发布版本

Storage:
    Tokens are saved to
        .larkagent/secrets/user_tokens/<app_id>.json
    which is covered by the repo's default-deny gitignore.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRET_BOT_DIR = REPO_ROOT / ".larkagent/secrets/feishu_bot"
SECRET_TOKEN_DIR = REPO_ROOT / ".larkagent/secrets/user_tokens"
SECRET_TOKEN_DIR.mkdir(parents=True, exist_ok=True)

CALLBACK_HOST = "localhost"
CALLBACK_PORT = 18765
CALLBACK_PATH = "/feishu/callback"
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"
AUTHORIZE_ENDPOINT = "https://open.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_ENDPOINT = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
DEFAULT_SCOPE = "im:message.send_as_user offline_access"

APP_FILES = {
    "application_agent": SECRET_BOT_DIR / "application_agent.env",
    "tech_lead": SECRET_BOT_DIR / "tech-lead-planner.env",
    "product_manager": SECRET_BOT_DIR / "product-manager-prd.env",
}


def _parse_env_file(path: Path) -> dict[str, str]:
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
    return raw


def _load_app_creds(prefix: str) -> tuple[str, str, dict]:
    path = APP_FILES.get(prefix)
    if not path or not path.exists():
        raise SystemExit(f"unknown app prefix {prefix!r}")
    raw = _parse_env_file(path)
    app_id = raw.get("AppID") or raw.get("APP_ID") or ""
    app_secret = raw.get("AppSecret") or raw.get("APP_SECRET") or ""
    if not app_id or not app_secret:
        raise SystemExit(f"AppID / AppSecret missing in {path}")
    return app_id, app_secret, raw


def _token_path(app_id: str) -> Path:
    return SECRET_TOKEN_DIR / f"{app_id}.json"


def _save_token(app_id: str, data: dict) -> None:
    enriched = {
        **data,
        "acquired_at": int(time.time()),
        "expires_at": int(time.time()) + int(data.get("expires_in", 0)),
        "refresh_expires_at": int(time.time())
        + int(data.get("refresh_token_expires_in", 0)),
    }
    path = _token_path(app_id)
    path.write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[auth] saved token → {path}")


def _load_token(app_id: str) -> dict | None:
    p = _token_path(app_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _authorize_url(app_id: str, scope: str, state: str = "feishuopc") -> str:
    q = urllib.parse.urlencode(
        {
            "app_id": app_id,
            "redirect_uri": REDIRECT_URI,
            "scope": scope,
            "state": state,
        }
    )
    return f"{AUTHORIZE_ENDPOINT}?{q}"


async def _exchange_code(app_id: str, app_secret: str, code: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as hc:
        r = await hc.post(
            TOKEN_ENDPOINT,
            json={
                "grant_type": "authorization_code",
                "client_id": app_id,
                "client_secret": app_secret,
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
        )
    payload = r.json()
    if payload.get("code") not in (0, None) and "access_token" not in payload:
        raise SystemExit(f"token exchange failed: {payload}")
    return payload


async def _refresh_token(app_id: str, app_secret: str, refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as hc:
        r = await hc.post(
            TOKEN_ENDPOINT,
            json={
                "grant_type": "refresh_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "refresh_token": refresh_token,
            },
        )
    payload = r.json()
    if payload.get("code") not in (0, None) and "access_token" not in payload:
        raise SystemExit(f"refresh failed: {payload}")
    return payload


async def _ensure_fresh_token(prefix: str) -> str:
    app_id, app_secret, _ = _load_app_creds(prefix)
    data = _load_token(app_id)
    if not data:
        raise SystemExit(
            f"No token for {prefix} (app_id={app_id}). Run auth-server first."
        )
    now = int(time.time())
    if data.get("expires_at", 0) - now > 60:
        return data["access_token"]

    refresh = data.get("refresh_token")
    if not refresh or data.get("refresh_expires_at", 0) - now < 60:
        raise SystemExit(
            "refresh_token expired or missing; re-run auth-server to re-authorize."
        )
    print(f"[auth] access_token expiring in {data.get('expires_at', 0) - now}s, refreshing...")
    fresh = await _refresh_token(app_id, app_secret, refresh)
    _save_token(app_id, fresh)
    return fresh["access_token"]


class _CallbackHandler(BaseHTTPRequestHandler):
    received: dict[str, str] | None = None

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        params = dict(urllib.parse.parse_qsl(parsed.query))
        type(self).received = params
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = (
            "<html><body style='font-family: sans-serif'>"
            f"<h2>Feishu OAuth callback received</h2>"
            f"<pre>{json.dumps(params, ensure_ascii=False, indent=2)}</pre>"
            "<p>You can close this tab and return to the terminal.</p>"
            "</body></html>"
        )
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *_a, **_kw) -> None:  # silence default stderr spam
        return


async def run_auth_server(prefix: str, scope: str) -> None:
    app_id, app_secret, _ = _load_app_creds(prefix)
    url = _authorize_url(app_id, scope)
    print("[auth] app:", prefix, "app_id:", app_id)
    print("[auth] redirect:", REDIRECT_URI)
    print(
        "[auth] Make sure this redirect URL is whitelisted on 飞书开放平台 → 应用安全 → 重定向 URL.\n"
        "        Also ensure the app has scope:", scope,
    )
    print("[auth] authorize URL:")
    print(url)

    server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), _CallbackHandler)
    thread = Thread(target=server.serve_forever, name="feishu-oauth-cb", daemon=True)
    thread.start()

    try:
        webbrowser.open(url)
    except Exception:
        pass

    print(f"[auth] waiting for callback on {REDIRECT_URI} ... (Ctrl+C to abort)")
    try:
        while _CallbackHandler.received is None:
            await asyncio.sleep(0.25)
    finally:
        server.shutdown()

    params = _CallbackHandler.received or {}
    if "code" not in params:
        raise SystemExit(f"no code in callback params: {params}")

    print("[auth] got code, exchanging for tokens ...")
    token_payload = await _exchange_code(app_id, app_secret, params["code"])
    _save_token(app_id, token_payload)
    print("[auth] done. access_token expires_in =", token_payload.get("expires_in"))


async def run_auth_url(prefix: str, scope: str) -> None:
    app_id, _, _ = _load_app_creds(prefix)
    print(_authorize_url(app_id, scope))


async def run_auth_exchange(prefix: str, code: str) -> None:
    app_id, app_secret, _ = _load_app_creds(prefix)
    payload = await _exchange_code(app_id, app_secret, code)
    _save_token(app_id, payload)
    print("[auth] token stored.")


async def run_auth_status(prefix: str) -> None:
    app_id, _, _ = _load_app_creds(prefix)
    data = _load_token(app_id)
    if not data:
        print(f"[auth] no token saved for {prefix} (app_id={app_id})")
        return
    now = int(time.time())
    print(json.dumps(
        {
            "app_id": app_id,
            "access_token_present": bool(data.get("access_token")),
            "scope": data.get("scope"),
            "token_type": data.get("token_type"),
            "access_expires_in_s": data.get("expires_at", 0) - now,
            "refresh_expires_in_s": data.get("refresh_expires_at", 0) - now,
        },
        ensure_ascii=False,
        indent=2,
    ))


async def run_as_user(prefix: str, message: str) -> None:
    app_id, _, raw = _load_app_creds(prefix)

    app_agent_raw = _parse_env_file(APP_FILES["application_agent"])
    group_chat_id = app_agent_raw.get("APPLICATION_AGENT_GROUP_CHAT_ID", "")
    app_agent_open_id = app_agent_raw.get("APPLICATION_AGENT_OPEN_ID", "")
    if not group_chat_id:
        raise SystemExit("APPLICATION_AGENT_GROUP_CHAT_ID missing in application_agent.env")
    if not app_agent_open_id:
        raise SystemExit("APPLICATION_AGENT_OPEN_ID missing in application_agent.env")

    token = await _ensure_fresh_token(prefix)

    text = f'<at user_id="{app_agent_open_id}">Application delegate</at> {message}'
    print("[as-user] chat_id    =", group_chat_id)
    print("[as-user] text       =", text)

    async with httpx.AsyncClient(timeout=15) as hc:
        r = await hc.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json={
                "receive_id": group_chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )
    payload = r.json()
    print("[as-user] response   =", json.dumps(payload, ensure_ascii=False, indent=2))


def _parse_app_flag(argv: list[str]) -> tuple[str, list[str]]:
    prefix = "application_agent"
    rest = []
    for tok in argv:
        if tok.startswith("--app="):
            prefix = tok.split("=", 1)[1]
        else:
            rest.append(tok)
    return prefix, rest


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)

    mode = sys.argv[1]
    prefix, rest = _parse_app_flag(sys.argv[2:])

    if mode == "auth-server":
        scope = rest[0] if rest else DEFAULT_SCOPE
        asyncio.run(run_auth_server(prefix, scope))
    elif mode == "auth-url":
        scope = rest[0] if rest else DEFAULT_SCOPE
        asyncio.run(run_auth_url(prefix, scope))
    elif mode == "auth-exchange":
        if not rest:
            raise SystemExit("usage: auth-exchange <code> [--app=...]")
        asyncio.run(run_auth_exchange(prefix, rest[0]))
    elif mode == "auth-status":
        asyncio.run(run_auth_status(prefix))
    elif mode == "as-user":
        if not rest:
            raise SystemExit('usage: as-user "<message>" [--app=...]')
        asyncio.run(run_as_user(prefix, rest[0]))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
