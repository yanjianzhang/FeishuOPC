#!/usr/bin/env python3
"""Impersonation token freshness check.

Designed to be run periodically (cron) on the host where the
FeishuOPC agent runs — i.e. the same host where the token file is
refreshed by the agent's ``ImpersonationTokenService``. Reading the
token file locally is the only way to get a reliable TTL; poking
Feishu from a different host would require shipping the token out of
the trust boundary.

Behavior
--------
- Reads ``.larkagent/secrets/user_tokens/<impersonation_app_id>.json``
- Computes remaining lifetime of the ``refresh_token`` (which, once
  expired, can't be silently renewed — a human has to re-run OAuth).
- Prints a one-line summary to stdout (cron mail friendly).
- When state is WARN / CRITICAL / EXPIRED / MISSING, posts a Feishu
  alert into the configured chat using the **tech-lead bot**, with an
  ``@所有人``-free plain text (so multiple triggers don't spam @s).
- Exits with:
    0 — OK
    1 — WARN  (refresh_token within ``IMPERSONATION_WARN_DAYS``)
    2 — CRITICAL / EXPIRED / MISSING

Configuration
-------------
Reads the same ``Settings`` as the main agent (so
``application_agent.env`` feeds impersonation_app_id, and
``tech-lead-planner.env`` provides the alert bot credentials). Extra
knobs via env:

- ``IMPERSONATION_WARN_DAYS`` (default 2)
- ``IMPERSONATION_CRITICAL_DAYS`` (default 1)
- ``IMPERSONATION_ALERT_CHAT_ID`` (default: settings.application_agent_group_chat_id)
- ``IMPERSONATION_ALERT_ONLY_ON_CHANGE=1`` — only alert when status
  differs from the last run (state stored next to the token file).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from feishu_fastapi_sdk import FeishuAuthConfig  # noqa: E402

from feishu_agent.config import get_settings  # noqa: E402
from feishu_agent.runtime.managed_feishu_client import ManagedFeishuClient  # noqa: E402

logging.basicConfig(
    level=os.environ.get("IMPERSONATION_CHECK_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("impersonation-check")


STATE_OK = "OK"
STATE_WARN = "WARN"
STATE_CRITICAL = "CRITICAL"
STATE_EXPIRED = "EXPIRED"
STATE_MISSING = "MISSING"
STATE_INVALID = "INVALID"

EXIT_CODES = {
    STATE_OK: 0,
    STATE_WARN: 1,
    STATE_CRITICAL: 2,
    STATE_EXPIRED: 2,
    STATE_MISSING: 2,
    STATE_INVALID: 2,
}


@dataclass
class CheckReport:
    state: str
    app_id: str
    token_path: Path
    access_left_s: int | None
    refresh_left_s: int | None
    message: str

    def human_summary(self) -> str:
        def fmt(seconds: int | None) -> str:
            if seconds is None:
                return "n/a"
            if seconds <= 0:
                return f"expired ({seconds}s)"
            days = seconds / 86400
            if days >= 1:
                return f"{days:.1f}d ({seconds}s)"
            hours = seconds / 3600
            return f"{hours:.1f}h ({seconds}s)"

        return (
            f"[{self.state}] app_id={self.app_id} path={self.token_path} "
            f"access_left={fmt(self.access_left_s)} "
            f"refresh_left={fmt(self.refresh_left_s)} :: {self.message}"
        )


def _resolve_token_path(settings: object) -> tuple[str, Path]:
    app_id = (getattr(settings, "impersonation_app_id", "") or "").strip()
    if not app_id:
        raise RuntimeError("impersonation_app_id is empty; cannot locate token file")
    token_dir = Path(getattr(settings, "impersonation_token_dir", ".larkagent/secrets/user_tokens"))
    if not token_dir.is_absolute():
        repo_root = Path(getattr(settings, "app_repo_root", ".") or ".")
        token_dir = repo_root / token_dir
    return app_id, token_dir / f"{app_id}.json"


def _build_report(settings: object, warn_s: int, critical_s: int) -> CheckReport:
    app_id, token_path = _resolve_token_path(settings)

    if not token_path.exists():
        return CheckReport(
            state=STATE_MISSING,
            app_id=app_id,
            token_path=token_path,
            access_left_s=None,
            refresh_left_s=None,
            message=(
                "token file missing — click the authorize link below to mint a new one "
                "(the callback microservice will write it directly to this host)"
            ),
        )
    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return CheckReport(
            state=STATE_INVALID,
            app_id=app_id,
            token_path=token_path,
            access_left_s=None,
            refresh_left_s=None,
            message=f"failed to parse token file: {exc}",
        )

    now = int(time.time())
    access_left = int(data.get("expires_at", 0)) - now
    refresh_left = int(data.get("refresh_expires_at", 0)) - now

    if refresh_left <= 0:
        state = STATE_EXPIRED
        msg = "refresh_token expired — click the authorize link to re-authorize"
    elif refresh_left <= critical_s:
        state = STATE_CRITICAL
        msg = f"refresh_token expires within {critical_s}s — re-authorize ASAP"
    elif refresh_left <= warn_s:
        state = STATE_WARN
        msg = f"refresh_token expires within {warn_s}s — schedule re-authorization"
    else:
        state = STATE_OK
        msg = "token healthy"

    return CheckReport(
        state=state,
        app_id=app_id,
        token_path=token_path,
        access_left_s=access_left,
        refresh_left_s=refresh_left,
        message=msg,
    )


def _state_file_for(token_path: Path) -> Path:
    return token_path.with_suffix(token_path.suffix + ".last-alert-state")


def _load_last_state(token_path: Path) -> str | None:
    state_file = _state_file_for(token_path)
    if not state_file.exists():
        return None
    try:
        return state_file.read_text(encoding="utf-8").strip() or None
    except Exception:
        return None


def _save_state(token_path: Path, state: str) -> None:
    state_file = _state_file_for(token_path)
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(state, encoding="utf-8")
    except Exception:
        logger.exception("failed to persist last alert state")


async def _send_feishu_alert(settings: object, chat_id: str, report: CheckReport) -> bool:
    tl_app_id = getattr(settings, "tech_lead_feishu_bot_app_id", "") or getattr(
        settings, "feishu_bot_app_id", ""
    )
    tl_app_secret = getattr(settings, "tech_lead_feishu_bot_app_secret", "") or getattr(
        settings, "feishu_bot_app_secret", ""
    )
    if not tl_app_id or not tl_app_secret:
        logger.warning("tech-lead bot credentials unavailable — skipping Feishu alert")
        return False

    icon = {
        STATE_WARN: "⚠️",
        STATE_CRITICAL: "🛑",
        STATE_EXPIRED: "💥",
        STATE_MISSING: "❓",
        STATE_INVALID: "❓",
    }.get(report.state, "·")

    def _fmt_days(seconds: int | None) -> str:
        if seconds is None:
            return "n/a"
        if seconds <= 0:
            return "已过期"
        days = seconds / 86400
        if days >= 1:
            return f"约 {days:.1f} 天"
        hours = seconds / 3600
        return f"约 {hours:.1f} 小时"

    authorize_url = os.environ.get(
        "IMPERSONATION_AUTHORIZE_URL",
        "http://127.0.0.1:18765/feishu/authorize?app=application_agent",
    )

    text = (
        f"{icon} 飞书真人代发 token 状态 = {report.state}\n"
        f"app_id = {report.app_id}\n"
        f"access_token 剩余: {_fmt_days(report.access_left_s)}（服务会自动 refresh）\n"
        f"refresh_token 剩余: {_fmt_days(report.refresh_left_s)}（过期后必须人工重新授权）\n"
        f"说明: {report.message}\n"
        f"一键续期: {authorize_url}\n"
        f"（链接会跳转到飞书授权页，授权后 token 会直接写到服务器，无需再跑本机命令）"
    )

    client = ManagedFeishuClient(
        FeishuAuthConfig(app_id=tl_app_id, app_secret=tl_app_secret),
        default_internal_token_kind="tenant",
    )
    try:
        await client.request(
            "POST",
            "/open-apis/im/v1/messages?receive_id_type=chat_id",
            json_body={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )
        return True
    except Exception as exc:
        logger.exception("feishu alert failed: %s", exc)
        return False


async def _main_async(args: argparse.Namespace) -> int:
    settings = get_settings()

    if not getattr(settings, "impersonation_enabled", True):
        print("[SKIP] impersonation_enabled=false — nothing to check")
        return 0

    warn_s = int(args.warn_days * 86400)
    critical_s = int(args.critical_days * 86400)
    report = _build_report(settings, warn_s=warn_s, critical_s=critical_s)
    print(report.human_summary(), flush=True)

    if args.dry_run:
        return EXIT_CODES.get(report.state, 2)

    if report.state == STATE_OK:
        _save_state(report.token_path, report.state)
        return 0

    if args.only_on_change:
        last = _load_last_state(report.token_path)
        if last == report.state:
            logger.info("state unchanged since last run (%s); suppressing alert", last)
            return EXIT_CODES.get(report.state, 2)

    chat_id = (
        args.alert_chat_id
        or os.environ.get("IMPERSONATION_ALERT_CHAT_ID")
        or getattr(settings, "application_agent_group_chat_id", "")
    )
    if not chat_id:
        logger.warning("no alert chat configured; skipping Feishu post")
    else:
        sent = await _send_feishu_alert(settings, chat_id, report)
        if sent:
            logger.info("feishu alert delivered to %s", chat_id)
        else:
            logger.warning("feishu alert could not be delivered")

    _save_state(report.token_path, report.state)
    return EXIT_CODES.get(report.state, 2)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    def _env_float(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--warn-days",
        type=float,
        default=_env_float("IMPERSONATION_WARN_DAYS", 2.0),
        help="Warn when refresh_token TTL falls below this many days (default 2).",
    )
    parser.add_argument(
        "--critical-days",
        type=float,
        default=_env_float("IMPERSONATION_CRITICAL_DAYS", 1.0),
        help="Escalate to CRITICAL below this many days (default 1).",
    )
    parser.add_argument(
        "--alert-chat-id",
        default=None,
        help="Override the chat id that receives WARN/CRITICAL alerts.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print status and exit; do not post any Feishu message.",
    )
    parser.add_argument(
        "--only-on-change",
        action="store_true",
        default=os.environ.get("IMPERSONATION_ALERT_ONLY_ON_CHANGE", "") in ("1", "true", "yes"),
        help="Only send an alert when the state differs from the previous run.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
