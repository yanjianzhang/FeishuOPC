"""Feishu OAuth callback microservice.

Purpose
-------
When a user's ``refresh_token`` is about to expire (or is missing),
operators previously had to SSH to their laptop, run
``spikes/probe_as_user.py auth-server --app=application_agent`` and
click through a local-only callback.  That round-trip is painful on
mobile and makes the alert messages less useful because the "fix it"
path is not a plain hyperlink.

This service replaces that with a public HTTPS endpoint (fronted by
Nginx + Certbot on your ``OAUTH_CALLBACK_PUBLIC_URL`` host) that:

- ``GET /feishu/authorize?app=<prefix>`` — builds the Feishu authorize
  URL for the selected app and 302-redirects the browser to it.
- ``GET /feishu/callback?code=...&state=...`` — exchanges the
  authorization ``code`` for ``access_token`` + ``refresh_token`` and
  writes the enriched JSON payload atomically to
  ``{impersonation_token_dir}/{app_id}.json``.  Returns an HTML page
  confirming the new expiry so the user knows it worked.
- ``GET /healthz`` — systemd / nginx liveness probe.

Design notes
------------
- Runs as an unprivileged process on ``127.0.0.1:<port>`` (Nginx
  reverse-proxies HTTPS traffic to it).  It never writes anywhere
  except ``impersonation_token_dir`` and only reads a narrow set of
  secrets under ``.larkagent/secrets/feishu_bot/*.env``.
- ``state`` is a short-lived in-memory nonce that also encodes which
  app the operator chose.  We do not persist it across restarts — if
  you restart the service mid-auth, just click Authorize again.
- The token file is written via a ``.tmp`` + ``os.replace`` to match
  ``ImpersonationTokenService._write_atomic`` (so a concurrent read by
  the agent never sees a truncated file).
"""

from __future__ import annotations

import html
import json
import logging
import os
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from feishu_agent.config import get_settings

logger = logging.getLogger(__name__)

AUTHORIZE_ENDPOINT = "https://open.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_ENDPOINT = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
DEFAULT_SCOPE = "im:message.send_as_user offline_access"

# App prefix → env file. Mirrors spikes/probe_as_user.py::APP_FILES so
# operators see the same vocabulary ("application_agent", "tech_lead",
# "product_manager") in the authorize URL that the probe uses.
APP_ENV_FILES = {
    "application_agent": ".larkagent/secrets/feishu_bot/application_agent.env",
    "tech_lead": ".larkagent/secrets/feishu_bot/tech-lead-planner.env",
    "product_manager": ".larkagent/secrets/feishu_bot/product-manager-prd.env",
}

_STATE_TTL_SECONDS = 600  # 10 minutes is plenty for a single auth click.
_STATE_MAX_ENTRIES = 10000  # hard cap so unauthenticated abuse can't grow memory

# Keys whose values must never appear in logs even on exchange failure.
_SENSITIVE_LOG_KEYS = {"access_token", "refresh_token", "client_secret", "code"}


def _redact_for_log(payload: Any) -> Any:
    """Return a shallow copy of ``payload`` with sensitive values masked."""
    if not isinstance(payload, dict):
        return payload
    return {
        k: ("***redacted***" if k in _SENSITIVE_LOG_KEYS and v else v)
        for k, v in payload.items()
    }


@dataclass
class _AppCreds:
    prefix: str
    app_id: str
    app_secret: str


def _parse_env_file(path: Path) -> dict[str, str]:
    raw: dict[str, str] = {}
    if not path.exists():
        return raw
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line and "=" not in line.split(":", 1)[0]:
            key, value = line.split(":", 1)
        elif "=" in line:
            key, value = line.split("=", 1)
        else:
            continue
        raw[key.strip()] = value.strip().strip('"').strip("'")
    return raw


def _resolve_repo_root() -> Path:
    settings = get_settings()
    root = getattr(settings, "app_repo_root", None)
    if root:
        return Path(root)
    env_root = os.environ.get("APP_REPO_ROOT")
    if env_root:
        return Path(env_root)
    return Path(__file__).resolve().parents[2]


def _load_app_creds(prefix: str) -> _AppCreds:
    rel = APP_ENV_FILES.get(prefix)
    if not rel:
        raise HTTPException(status_code=400, detail=f"unknown app prefix {prefix!r}")
    path = _resolve_repo_root() / rel
    raw = _parse_env_file(path)
    app_id = (raw.get("AppID") or raw.get("APP_ID") or "").strip()
    app_secret = (raw.get("AppSecret") or raw.get("APP_SECRET") or "").strip()
    if not app_id or not app_secret:
        raise HTTPException(
            status_code=500,
            detail=f"AppID/AppSecret missing in {path}; cannot start OAuth for {prefix!r}",
        )
    return _AppCreds(prefix=prefix, app_id=app_id, app_secret=app_secret)


def _token_dir() -> Path:
    settings = get_settings()
    raw = Path(getattr(settings, "impersonation_token_dir", ".larkagent/secrets/user_tokens"))
    if raw.is_absolute():
        return raw
    return _resolve_repo_root() / raw


def _token_path_for(app_id: str) -> Path:
    return _token_dir() / f"{app_id}.json"


def _write_token_atomic(app_id: str, payload: dict[str, Any]) -> Path:
    now = int(time.time())
    enriched = {
        **payload,
        "acquired_at": now,
        "expires_at": now + int(payload.get("expires_in", 0) or 0),
        "refresh_expires_at": now + int(payload.get("refresh_token_expires_in", 0) or 0),
    }
    path = _token_path_for(app_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(enriched, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _public_redirect_uri() -> str:
    raw = os.environ.get("OAUTH_CALLBACK_PUBLIC_URL", "").strip()
    if raw:
        return raw.rstrip("/")
    return "http://127.0.0.1:18765/feishu/callback"


def create_app() -> FastAPI:
    # IMPORTANT: this service is designed to run single-worker. ``state_store``
    # below is an in-process dict; if uvicorn is launched with ``--workers N``
    # (N>1) each worker holds its own store and OAuth callbacks will land on
    # a worker that doesn't know the state nonce. The shipped systemd unit
    # does not pass ``--workers`` (so defaults to 1); keep it that way.
    app = FastAPI(
        title="FeishuOPC OAuth callback",
        description=(
            "Public HTTPS endpoint that mints + persists Feishu user "
            "access/refresh tokens for the impersonation 'send as user' path."
        ),
        version="1.0.0",
    )

    # state -> (app_prefix, issued_at)
    state_store: dict[str, tuple[str, float]] = {}

    def _mint_state(prefix: str) -> str:
        nonce = secrets.token_urlsafe(24)
        now = time.time()
        # Garbage-collect expired states so this dict never grows unbounded
        # under steady usage.
        for k in [k for k, (_, ts) in state_store.items() if now - ts > _STATE_TTL_SECONDS]:
            state_store.pop(k, None)
        # Hard cap against unauthenticated abuse (millions of /authorize hits
        # in the TTL window). Evict the oldest entries FIFO if we're over.
        if len(state_store) >= _STATE_MAX_ENTRIES:
            for k in sorted(state_store, key=lambda key: state_store[key][1])[
                : len(state_store) - _STATE_MAX_ENTRIES + 1
            ]:
                state_store.pop(k, None)
        state_store[nonce] = (prefix, now)
        return nonce

    def _consume_state(state: str) -> str | None:
        entry = state_store.pop(state, None)
        if not entry:
            return None
        prefix, issued_at = entry
        if time.time() - issued_at > _STATE_TTL_SECONDS:
            return None
        return prefix

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    async def index() -> HTMLResponse:
        body = (
            "<html><body style='font-family:sans-serif;max-width:640px;margin:40px auto'>"
            "<h2>FeishuOPC OAuth callback</h2>"
            "<p>This service persists Feishu user access / refresh tokens "
            "used by the 'send as user' impersonation path.</p>"
            "<p>Start authorization:</p>"
            "<ul>"
            "<li><a href='/feishu/authorize?app=application_agent'>application_agent</a>"
            " (delegate app)</li>"
            "<li><a href='/feishu/authorize?app=tech_lead'>tech_lead</a></li>"
            "<li><a href='/feishu/authorize?app=product_manager'>product_manager</a></li>"
            "</ul>"
            "</body></html>"
        )
        return HTMLResponse(body)

    @app.get("/feishu/authorize")
    async def authorize(
        app: str = Query("application_agent", description="App prefix"),
        scope: str = Query(DEFAULT_SCOPE, description="OAuth scopes"),
    ) -> RedirectResponse:
        creds = _load_app_creds(app)
        state = _mint_state(app)
        q = urllib.parse.urlencode(
            {
                "app_id": creds.app_id,
                "redirect_uri": _public_redirect_uri(),
                "scope": scope,
                "state": state,
            }
        )
        url = f"{AUTHORIZE_ENDPOINT}?{q}"
        logger.info("oauth.authorize prefix=%s app_id=%s state=%s", app, creds.app_id, state[:8])
        return RedirectResponse(url, status_code=302)

    @app.get("/feishu/callback")
    async def callback(
        code: str | None = Query(None),
        state: str | None = Query(None),
        error: str | None = Query(None),
        error_description: str | None = Query(None),
    ) -> HTMLResponse:
        if error:
            return HTMLResponse(
                "<h2>Feishu returned an error</h2><p>"
                f"{html.escape(error)}: {html.escape(error_description or '')}"
                "</p>",
                status_code=400,
            )
        if not code or not state:
            raise HTTPException(status_code=400, detail="missing code/state")
        prefix = _consume_state(state)
        if not prefix:
            raise HTTPException(status_code=400, detail="invalid or expired state")

        creds = _load_app_creds(prefix)
        async with httpx.AsyncClient(timeout=20) as hc:
            r = await hc.post(
                TOKEN_ENDPOINT,
                json={
                    "grant_type": "authorization_code",
                    "client_id": creds.app_id,
                    "client_secret": creds.app_secret,
                    "code": code,
                    "redirect_uri": _public_redirect_uri(),
                },
            )
        payload = r.json()
        if "access_token" not in payload:
            redacted = _redact_for_log(payload)
            logger.warning("oauth.exchange_failed prefix=%s payload=%s", prefix, redacted)
            return HTMLResponse(
                "<h2>Token exchange failed</h2><pre>"
                + html.escape(json.dumps(redacted, ensure_ascii=False, indent=2))
                + "</pre>",
                status_code=502,
            )

        path = _write_token_atomic(creds.app_id, payload)
        access_expires = int(payload.get("expires_in", 0) or 0)
        refresh_expires = int(payload.get("refresh_token_expires_in", 0) or 0)
        logger.info(
            "oauth.persisted prefix=%s app_id=%s path=%s access_in=%ss refresh_in=%ss",
            prefix,
            creds.app_id,
            path,
            access_expires,
            refresh_expires,
        )

        body = (
            "<html><body style='font-family:sans-serif;max-width:640px;margin:40px auto'>"
            "<h2 style='color:#059669'>授权成功 ✅</h2>"
            f"<p>app = <code>{html.escape(prefix)}</code> / app_id = "
            f"<code>{html.escape(creds.app_id)}</code></p>"
            f"<p>access_token 有效期：约 {access_expires // 3600} 小时</p>"
            f"<p>refresh_token 有效期：约 {refresh_expires // 86400} 天</p>"
            f"<p>token 已写入 <code>{html.escape(str(path))}</code>（600 权限）</p>"
            "<p>可以关闭此页面，FeishuOPC 的真人代发链路会在下一次请求时自动使用新 token。</p>"
            "</body></html>"
        )
        return HTMLResponse(body)

    return app


app = create_app()
