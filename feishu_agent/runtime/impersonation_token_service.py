"""Feishu "send as user" impersonation token service.

Context
-------
Feishu does not deliver ``@mention`` events from one bot to another —
Some downstream delegate bots only trigger when a **real user**
mentions them. To work around that, we send delegate messages with a
``user_access_token`` belonging to an authorized human operator.
Feishu then attributes the message to that user, and the downstream bot
responds as if a teammate had pinged it.

Token lifecycle
---------------
Tokens are obtained via the OAuth2 authorization-code flow (see
``spikes/probe_as_user.py``) and persisted as JSON files at
``.larkagent/secrets/user_tokens/<app_id>.json``. This service loads
the file, refreshes the ``access_token`` before it expires, writes the
new token back atomically, and returns a currently-valid access token
to callers. When the ``refresh_token`` itself is expired (~7 days) the
service returns ``None`` and logs a WARNING prompting operators to
re-run the probe's ``auth-server`` flow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TOKEN_ENDPOINT = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"

# Refresh the access_token when fewer than this many seconds remain.
# Feishu access_tokens live ~7200s; 300s is the "comfortable" refresh
# window used by most clients.
_ACCESS_REFRESH_THRESHOLD_S = 300

# Treat refresh_token as expired when fewer than this many seconds
# remain. Feishu refresh_tokens live ~604800s (7 days); 3600s means we
# stop trusting it roughly an hour before hard expiry so the caller's
# first failure reason is unambiguously "re-auth needed" rather than a
# race with Feishu expiring the token mid-refresh.
_REFRESH_TOKEN_GRACE_S = 3600


class ImpersonationTokenService:
    """Load / refresh / persist a Feishu user_access_token for one app."""

    def __init__(self, app_id: str, app_secret: str, token_path: Path) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._token_path = Path(token_path)
        self._lock = asyncio.Lock()
        self._cached: dict[str, Any] | None = None
        self._last_error: str | None = None

    @property
    def app_id(self) -> str:
        return self._app_id

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def token_path(self) -> Path:
        return self._token_path

    def _load_from_disk(self) -> dict[str, Any] | None:
        if not self._token_path.exists():
            return None
        try:
            return json.loads(self._token_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self._last_error = f"failed to read {self._token_path}: {exc}"
            logger.exception("impersonation: failed to read token file")
            return None

    def _write_atomic(self, data: dict[str, Any]) -> None:
        self._token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._token_path.with_suffix(self._token_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._token_path)

    def _token_needs_refresh(self, token: dict[str, Any]) -> bool:
        now = int(time.time())
        return (token.get("expires_at", 0) - now) <= _ACCESS_REFRESH_THRESHOLD_S

    def _refresh_token_usable(self, token: dict[str, Any]) -> bool:
        now = int(time.time())
        if not token.get("refresh_token"):
            return False
        return (token.get("refresh_expires_at", 0) - now) > _REFRESH_TOKEN_GRACE_S

    async def _call_refresh(self, refresh_token: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15) as hc:
            r = await hc.post(
                _TOKEN_ENDPOINT,
                json={
                    "grant_type": "refresh_token",
                    "client_id": self._app_id,
                    "client_secret": self._app_secret,
                    "refresh_token": refresh_token,
                },
            )
        payload = r.json()
        if "access_token" not in payload:
            raise RuntimeError(f"refresh failed: {payload}")
        now = int(time.time())
        payload.setdefault("acquired_at", now)
        payload["expires_at"] = now + int(payload.get("expires_in", 0))
        payload["refresh_expires_at"] = now + int(payload.get("refresh_token_expires_in", 0))
        return payload

    async def get_access_token(self) -> str | None:
        """Return a currently-valid user access_token, refreshing if
        needed. Returns ``None`` (and records ``last_error``) when no
        token is stored or when the stored refresh_token itself has
        expired and a fresh OAuth consent is required.
        """

        async with self._lock:
            token = self._cached or self._load_from_disk()
            if not token:
                self._last_error = (
                    f"no user token at {self._token_path}; run spikes/probe_as_user.py auth-server"
                )
                logger.warning("impersonation: %s", self._last_error)
                return None

            if not self._token_needs_refresh(token):
                self._cached = token
                return token.get("access_token") or None

            if not self._refresh_token_usable(token):
                self._last_error = (
                    "user refresh_token expired; re-run auth-server to re-authorize"
                )
                logger.warning("impersonation: %s (token_path=%s)", self._last_error, self._token_path)
                return None

            try:
                fresh = await self._call_refresh(token["refresh_token"])
            except Exception as exc:
                self._last_error = f"refresh call failed: {exc}"
                logger.exception("impersonation: refresh call failed")
                return None

            self._write_atomic(fresh)
            self._cached = fresh
            self._last_error = None
            logger.info(
                "impersonation: refreshed access_token, expires_in=%ss",
                fresh.get("expires_in"),
            )
            return fresh.get("access_token") or None
