"""Unit tests for feishu_ws_main helpers.

Focus: the unmentioned-message bypass for PendingAction-backed
conversations. The ws_main module has import-time side effects
(asyncio loop thread, bot-open-id fetch) so we test the helper by
importing the module under carefully-prepared env vars/monkeypatches.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

from feishu_agent.team.pending_action_service import (
    PendingAction,
    PendingActionService,
)


def _reload_ws_main(monkeypatch: pytest.MonkeyPatch, repo_root: Path, role_name: str):
    """Import feishu_ws_main fresh, pointed at a test repo_root.

    ``settings.app_repo_root`` is resolved through ``_discover_repo_root``
    which insists on finding ``project-adapters`` + ``feishu_agent`` at
    the root. Rather than fake that full layout, we monkeypatch the
    reimported module's ``settings`` snapshot directly — that's exactly
    how the helper reads it at runtime.
    """
    monkeypatch.setenv("LARK_ROLE_NAME", role_name)
    monkeypatch.setenv("TECH_LEAD_FEISHU_BOT_APP_ID", "app-test")
    monkeypatch.setenv("TECH_LEAD_FEISHU_BOT_APP_SECRET", "secret-test")
    monkeypatch.setenv("PRODUCT_MANAGER_FEISHU_BOT_APP_ID", "app-pm")
    monkeypatch.setenv("PRODUCT_MANAGER_FEISHU_BOT_APP_SECRET", "secret-pm")

    from feishu_agent.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]

    for mod in ("feishu_agent.feishu_ws_main",):
        sys.modules.pop(mod, None)
    ws_main = importlib.import_module("feishu_agent.feishu_ws_main")
    monkeypatch.setattr(ws_main.settings, "app_repo_root", str(repo_root))
    return ws_main


def _pending_dir(repo_root: Path) -> Path:
    # Must match ``settings.techbot_run_log_dir`` (default "data/techbot-runs").
    return repo_root / "data" / "techbot-runs" / "pending"


def _save_pending(
    repo_root: Path,
    *,
    trace_id: str,
    chat_id: str,
    role_name: str,
    action_type: str,
) -> None:
    service = PendingActionService(_pending_dir(repo_root))
    service.save(
        PendingAction(
            trace_id=trace_id,
            chat_id=chat_id,
            role_name=role_name,
            action_type=action_type,
            action_args={},
        )
    )


def test_pending_bypass_matches_write_progress_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """write_progress_sync is the most common waiting-for-reply flow
    (TechLead + PM both use it via request_confirmation). Unmentioned
    replies in the same chat must reach the bot."""
    ws_main = _reload_ws_main(monkeypatch, tmp_path, "tech-lead-planner")
    _save_pending(
        tmp_path,
        trace_id="pending-wps-001",
        chat_id="chat-abc",
        role_name="tech_lead",  # the internal short name stored on disk
        action_type="write_progress_sync",
    )
    assert ws_main._chat_has_pending_for_this_role("chat-abc") is True


def test_pending_bypass_matches_force_sync_to_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    ws_main = _reload_ws_main(monkeypatch, tmp_path, "tech-lead-planner")
    _save_pending(
        tmp_path,
        trace_id="pending-fs-001",
        chat_id="chat-fs",
        role_name="tech_lead",
        action_type="force_sync_to_remote",
    )
    assert ws_main._chat_has_pending_for_this_role("chat-fs") is True


def test_pending_bypass_rejects_wrong_bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Safety rail: a pending for product_manager must NOT bypass the
    tech_lead bot, even though both share the same pending dir when
    deployed in the same app_repo_root (tests use the default layout)."""
    ws_main = _reload_ws_main(monkeypatch, tmp_path, "tech-lead-planner")
    _save_pending(
        tmp_path,
        trace_id="pending-pm-001",
        chat_id="chat-mixed",
        role_name="product_manager",
        action_type="write_progress_sync",
    )
    assert ws_main._chat_has_pending_for_this_role("chat-mixed") is False


def test_pending_bypass_matches_pm_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Complement of the rejection case: product-manager-prd deployment
    picks up its own pending files."""
    ws_main = _reload_ws_main(monkeypatch, tmp_path, "product-manager-prd")
    _save_pending(
        tmp_path,
        trace_id="pending-pm-ok",
        chat_id="chat-pm",
        role_name="product_manager",
        action_type="write_progress_sync",
    )
    assert ws_main._chat_has_pending_for_this_role("chat-pm") is True


def test_pending_bypass_legacy_empty_role_name_matches_canonical_bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Older pending files without a ``role_name`` are accepted for the
    canonical TL / PM bots (backward-compat with pre-role-field writes),
    so any in-flight confirmation at the moment of upgrade still flows."""
    ws_main = _reload_ws_main(monkeypatch, tmp_path, "tech-lead-planner")
    _save_pending(
        tmp_path,
        trace_id="pending-legacy-001",
        chat_id="chat-legacy",
        role_name="",
        action_type="write_progress_sync",
    )
    assert ws_main._chat_has_pending_for_this_role("chat-legacy") is True


def test_pending_bypass_legacy_empty_role_name_rejected_for_default_bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """M-1 regression: the ``default`` fallback bot must NOT inherit
    legacy pending files with empty role_name. In a multi-bot
    deployment that would let the default catch-all answer on behalf
    of whichever bot actually created the pending.

    Constructing a real ``default`` BOT_CONTEXT requires suppressing
    all TL/PM secret files (which leak in from the dev checkout), so
    we reach in and swap ``BOT_CONTEXT`` post-import — that is the
    exact object the helper reads at runtime.
    """
    ws_main = _reload_ws_main(monkeypatch, tmp_path, "tech-lead-planner")
    from feishu_agent.runtime.feishu_runtime_service import FeishuBotContext

    monkeypatch.setattr(
        ws_main,
        "BOT_CONTEXT",
        FeishuBotContext(
            bot_name="default",
            app_id="app-default",
            app_secret="secret-default",
            verification_token=None,
            encrypt_key=None,
        ),
    )

    _save_pending(
        tmp_path,
        trace_id="pending-legacy-default",
        chat_id="chat-default-legacy",
        role_name="",
        action_type="write_progress_sync",
    )
    assert ws_main._chat_has_pending_for_this_role("chat-default-legacy") is False


def test_pending_bypass_ttl_expires_stale_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """H-1 regression: a pending file older than ``PENDING_BYPASS_TTL_SECONDS``
    must NOT keep waving through unmentioned messages, even though the
    file is still on disk (only confirm/cancel deletes it)."""
    ws_main = _reload_ws_main(monkeypatch, tmp_path, "tech-lead-planner")
    _save_pending(
        tmp_path,
        trace_id="pending-ttl-stale",
        chat_id="chat-ttl",
        role_name="tech_lead",
        action_type="write_progress_sync",
    )
    pending_file = _pending_dir(tmp_path) / "pending-ttl-stale.json"
    # Back-date the file WAY past the default TTL.
    aged = pending_file.stat().st_mtime - (365 * 24 * 3600)
    os.utime(pending_file, (aged, aged))

    assert ws_main._chat_has_pending_for_this_role("chat-ttl") is False


def test_pending_bypass_ttl_configurable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A shorter ``LARK_PENDING_BYPASS_TTL_SECONDS`` env var must tighten
    the window. Ops can dial this down in noisy group-chat deployments."""
    monkeypatch.setenv("LARK_PENDING_BYPASS_TTL_SECONDS", "1")
    ws_main = _reload_ws_main(monkeypatch, tmp_path, "tech-lead-planner")
    _save_pending(
        tmp_path,
        trace_id="pending-ttl-short",
        chat_id="chat-ttl-short",
        role_name="tech_lead",
        action_type="write_progress_sync",
    )
    pending_file = _pending_dir(tmp_path) / "pending-ttl-short.json"
    aged = pending_file.stat().st_mtime - 10  # 10s ago, TTL is 1s
    os.utime(pending_file, (aged, aged))

    assert ws_main._chat_has_pending_for_this_role("chat-ttl-short") is False


def test_pending_bypass_ttl_fresh_pending_still_bypasses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Sanity check: a brand-new pending file is well under TTL and
    bypasses as before."""
    ws_main = _reload_ws_main(monkeypatch, tmp_path, "tech-lead-planner")
    _save_pending(
        tmp_path,
        trace_id="pending-ttl-fresh",
        chat_id="chat-ttl-fresh",
        role_name="tech_lead",
        action_type="write_progress_sync",
    )
    assert ws_main._chat_has_pending_for_this_role("chat-ttl-fresh") is True


def test_pending_bypass_returns_false_when_no_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    ws_main = _reload_ws_main(monkeypatch, tmp_path, "tech-lead-planner")
    assert ws_main._chat_has_pending_for_this_role("chat-none") is False


def test_pending_bypass_empty_chat_id_is_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    ws_main = _reload_ws_main(monkeypatch, tmp_path, "tech-lead-planner")
    assert ws_main._chat_has_pending_for_this_role(None) is False
    assert ws_main._chat_has_pending_for_this_role("") is False
