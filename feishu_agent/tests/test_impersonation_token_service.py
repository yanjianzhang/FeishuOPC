from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from feishu_agent.runtime.impersonation_token_service import ImpersonationTokenService


def _write_token(path: Path, **overrides: object) -> None:
    now = int(time.time())
    data: dict = {
        "access_token": "u-a-t-cached",
        "refresh_token": "u-r-t-cached",
        "expires_in": 7200,
        "refresh_token_expires_in": 604800,
        "expires_at": now + 7200,
        "refresh_expires_at": now + 604800,
        "acquired_at": now,
    }
    data.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.asyncio
async def test_returns_cached_token_when_fresh(tmp_path: Path) -> None:
    token_path = tmp_path / "cli_test.json"
    _write_token(token_path)
    svc = ImpersonationTokenService("cli_test", "secret", token_path)
    tok = await svc.get_access_token()
    assert tok == "u-a-t-cached"
    assert svc.last_error is None


@pytest.mark.asyncio
async def test_missing_file_returns_none_with_hint(tmp_path: Path) -> None:
    token_path = tmp_path / "nope.json"
    svc = ImpersonationTokenService("cli_test", "secret", token_path)
    tok = await svc.get_access_token()
    assert tok is None
    assert "auth-server" in (svc.last_error or "")


@pytest.mark.asyncio
async def test_refreshes_when_access_near_expiry(tmp_path: Path) -> None:
    """Access_token within the refresh threshold triggers a refresh
    call; the new token is written back atomically and returned."""
    token_path = tmp_path / "cli_test.json"
    # access expires in 60s (< 300s threshold), refresh still healthy
    now = int(time.time())
    _write_token(
        token_path,
        expires_at=now + 60,
    )
    svc = ImpersonationTokenService("cli_test", "secret", token_path)

    async def _fake_refresh(refresh_token: str) -> dict:
        assert refresh_token == "u-r-t-cached"
        return {
            "access_token": "u-a-t-new",
            "refresh_token": "u-r-t-new",
            "expires_in": 7200,
            "refresh_token_expires_in": 604800,
            "expires_at": now + 7200,
            "refresh_expires_at": now + 604800,
            "acquired_at": now,
        }

    with patch.object(svc, "_call_refresh", AsyncMock(side_effect=_fake_refresh)):
        tok = await svc.get_access_token()

    assert tok == "u-a-t-new"
    persisted = json.loads(token_path.read_text(encoding="utf-8"))
    assert persisted["access_token"] == "u-a-t-new"
    assert persisted["refresh_token"] == "u-r-t-new"


@pytest.mark.asyncio
async def test_returns_none_when_refresh_token_expired(tmp_path: Path) -> None:
    token_path = tmp_path / "cli_test.json"
    now = int(time.time())
    _write_token(
        token_path,
        expires_at=now + 10,  # forces refresh attempt
        refresh_expires_at=now + 60,  # < 3600s grace — refuse
    )
    svc = ImpersonationTokenService("cli_test", "secret", token_path)
    tok = await svc.get_access_token()
    assert tok is None
    assert "re-authorize" in (svc.last_error or "").lower() or "re-run" in (svc.last_error or "")
