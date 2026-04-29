"""Pre-flight git sync + baseline capture for LLM-driven bots.

The tech-lead / PM agents run on a remote server against a cloned
``shared-repo`` of the customer project. Without an explicit sync
step the clone can drift behind the authoritative remote (GitHub),
and the LLM then happily answers questions using **stale** spec /
artefact files — this is the exact failure mode that blocked
``/speckit.plan`` in spring 2026.

This module is the executor-side enforcement of "always rebase
before you think":

- Called once at the start of each Feishu thread (cached for a
  short TTL keyed by ``bot_name + chat_id + thread_id``).
- Tries ``GitOpsService.sync_with_remote`` — fast-forward only, no
  merge, no rebase. Typed errors (dirty / diverged / no-upstream)
  are caught and turned into a non-fatal skip reason.
- Captures branch / HEAD / last-commit metadata regardless of
  whether sync succeeded, so the LLM always has a concrete baseline
  SHA to reason from.
- Emits a compact Chinese thread update ("🔄 git 已同步：…") so the
  human sees the baseline in Feishu.
- Produces a ``render_baseline_for_prompt`` block that the runtime
  concatenates onto the system prompt, so every LLM turn opens with
  the same baseline snapshot.

Intentionally **does not** call ``git_sync_remote`` as an LLM tool —
the LLM frequently "forgets" to call it, especially on plan / status
/ review flows where the main skill doc didn't list it. Hard-coding
the call here makes it invariant across every workflow.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from feishu_agent.team.pending_action_service import (
    PendingAction,
    PendingActionService,
)
from feishu_agent.tools.git_ops_service import (
    GitNoUpstreamError,
    GitOpsError,
    GitOpsService,
    GitSyncDirtyError,
    GitSyncDivergedError,
    GitSyncResult,
)

try:
    import fcntl  # POSIX-only; used to serialize checkout + sync across bot processes.

    _FCNTL_AVAILABLE = True
except ImportError:  # pragma: no cover - Windows dev machines
    fcntl = None  # type: ignore[assignment]
    _FCNTL_AVAILABLE = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


# ``base_branch_status`` values used on ``PreflightSnapshot``:
#   - ``None``            — caller did not request a base_branch (TL path).
#   - ``"already_on"``    — already on the requested base_branch; silent no-op.
#   - ``"switched"``      — worktree was switched onto the requested base_branch.
#   - ``"dirty_skip"``    — worktree was dirty, checkout refused, still on old branch.
#   - ``"missing_skip"``  — requested base_branch does not exist locally.
#   - ``"error_skip"``    — checkout / git probe errored; stayed on old branch.
#
# When the status is one of the ``_skip`` variants, ``branch`` on the
# snapshot is NOT the baseline the caller asked for, and callers
# (including ``render_baseline_for_prompt``) must surface that so the
# LLM doesn't silently act as if the realignment succeeded.
_BASE_BRANCH_SKIP_STATES = frozenset(
    {"dirty_skip", "missing_skip", "error_skip"}
)


@dataclass(frozen=True)
class PreflightSnapshot:
    """Baseline snapshot captured at the start of a Feishu thread.

    ``sync_status`` values:
    - ``up_to_date`` — local matches remote, no change.
    - ``fast_forwarded`` — pulled ``len(pulled_commits)`` commits.
    - ``ahead_no_action`` — local is ahead of remote (not pulled).
    - ``sync_skipped`` — sync refused or errored; ``skip_reason`` has why.
    - ``sync_unavailable`` — no GitOpsService wired (e.g. project
      has no policy entry). We still capture HEAD info.
    """

    project_id: str
    branch: str
    head_sha: str
    head_sha_short: str
    last_commit_subject: str
    last_commit_author: str
    last_commit_relative: str
    sync_status: str
    skip_reason: str | None
    pulled_commits: list[str] = field(default_factory=list)
    # True when this invocation actually ran the fetch/FF. False when
    # served from the per-thread cache — useful for callers that want
    # to suppress the "🔄 git 已同步" thread update on cache hits.
    synced_this_turn: bool = True
    # Role-specific baseline realignment — populated by
    # ``_ensure_base_branch``. ``base_branch_requested`` is what the
    # caller asked for (e.g. PM passes ``"main"``); ``base_branch_status``
    # captures the outcome. Both fields are ``None`` when no realignment
    # was requested (TL path), making the default a cheap equality check
    # in ``render_baseline_for_prompt``.
    base_branch_requested: str | None = None
    base_branch_status: str | None = None
    # When preflight detected local↔remote divergence and a pending
    # ``force_sync_to_remote`` action was enqueued (user must reply
    # "确认"/"取消"), this holds the trace_id so callers/renderers can
    # tell the LLM an outstanding confirm request exists — don't
    # re-prompt, don't pretend the sync succeeded.
    pending_force_sync_trace_id: str | None = None


@dataclass
class _CacheEntry:
    snapshot: PreflightSnapshot
    ts: float


_CACHE_TTL_SECONDS = 300.0
# Prune threshold — drop entries older than 4x TTL from the cache so
# long-lived processes don't accumulate stale entries. The cache is
# bounded naturally by the number of distinct threads the bot sees
# in a 20-minute window, which in practice is small.
_CACHE_PRUNE_AGE_SECONDS = _CACHE_TTL_SECONDS * 4

_CACHE: dict[str, _CacheEntry] = {}
_LOCK = threading.Lock()

# Per-root in-process lock, so multiple threads in the same bot
# process don't race each other on a shared-repo. (Cross-process
# races are handled by ``_repo_filelock`` below using fcntl.)
_INPROC_ROOT_LOCKS: dict[str, threading.Lock] = {}


def _inproc_lock_for(root: Path) -> threading.Lock:
    key = str(root.resolve())
    with _LOCK:
        lock = _INPROC_ROOT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _INPROC_ROOT_LOCKS[key] = lock
    return lock


@contextmanager
def _repo_filelock(root: Path) -> Iterator[None]:
    """Serialize checkout + sync across bot processes sharing a clone.

    PM-bot and TL-bot are separate systemd services pointing at the
    same ``shared-repo``; without this, concurrent inbound messages
    race on ``git checkout`` / ``git fetch`` and corrupt each other
    via ``.git/index.lock`` contention.

    Layering:
      1. In-process threading lock (always held) — cheap and covers
         multi-thread uvicorn workers inside one process.
      2. Cross-process ``fcntl.flock`` on ``.git/.feishu-preflight.lock``
         (best-effort; POSIX only). On platforms without ``fcntl`` we
         degrade to step 1 only, which is enough for dev machines.

    Also re-exported as ``repo_filelock`` for ``ArtifactPublishService``
    so doc commits + pushes serialize against the same preflight lock
    — prevents a PM publish from racing a concurrent TL preflight that
    might be mid-branch-switch on the shared clone.
    """
    inproc = _inproc_lock_for(root)
    inproc.acquire()
    lock_path = root / ".git" / ".feishu-preflight.lock"
    fd: int | None = None
    if _FCNTL_AVAILABLE:
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX)  # type: ignore[union-attr]
        except OSError:
            logger.warning(
                "repo_filelock: failed to acquire fcntl lock at %s; "
                "continuing with in-process lock only",
                lock_path,
                exc_info=True,
            )
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                fd = None
    try:
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)  # type: ignore[union-attr]
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        inproc.release()


# Public alias — services outside preflight (e.g. ArtifactPublishService)
# import this name rather than dunder-prefixed one to signal that the
# lock is a deliberately shared interface.
repo_filelock = _repo_filelock


def _thread_cache_key(
    *, bot_name: str, chat_id: str | None, thread_id: str | None
) -> str:
    return f"{bot_name}::{chat_id or ''}::{thread_id or ''}"


def _invalidate_cache_for_root(root: Path) -> None:
    """Drop every cached snapshot — used when ``_ensure_base_branch``
    actually mutates HEAD of the shared-repo.

    A bot's cached snapshot describes a branch that may no longer be
    checked out after a switch. We could tag each entry with its
    root and evict selectively, but in practice every bot process
    sees exactly one shared-repo, so flushing is simpler and correct.
    The operation is O(n) in cache size, which is bounded by the
    number of active threads (usually single digits).
    """
    with _LOCK:
        if _CACHE:
            logger.info(
                "preflight cache flushed after base-branch switch on %s "
                "(%d entries dropped)",
                root,
                len(_CACHE),
            )
            _CACHE.clear()


def _prune_cache_locked(now: float) -> None:
    expired = [
        k
        for k, entry in _CACHE.items()
        if now - entry.ts > _CACHE_PRUNE_AGE_SECONDS
    ]
    for k in expired:
        _CACHE.pop(k, None)


def reset_cache_for_tests() -> None:
    """Drop the preflight cache — unit tests use this to guarantee
    every test case runs a fresh sync."""
    with _LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


ThreadUpdateCallback = Callable[[str], None]


def run_preflight_sync(
    *,
    git_ops_service: GitOpsService | None,
    project_id: str,
    project_root: Path | None,
    bot_name: str,
    chat_id: str | None,
    thread_id: str | None,
    thread_update_fn: ThreadUpdateCallback | None = None,
    base_branch: str | None = None,
    now_fn: Callable[[], float] = time.monotonic,
    pending_action_service: PendingActionService | None = None,
    force_sync_target_branch: str = "main",
    force_sync_remote: str = "origin",
) -> PreflightSnapshot | None:
    """Sync the project repo with its remote and return a baseline snapshot.

    Returns ``None`` when no baseline can be captured (no project_root
    resolved / not a git checkout). In that case the caller should
    skip baseline injection — there's nothing meaningful to show.

    ``base_branch`` — optional. When set, we first try to switch the
    worktree onto that branch before syncing. This exists because the
    ``shared-repo`` on the server is shared across roles; when a
    role's semantic baseline is "latest merged main" (e.g. the PM
    exploring a new idea), we mustn't silently inherit whatever
    feature branch the tech-lead happened to leave checked out. The
    switch is best-effort: a dirty worktree or a missing local
    branch degrades to a warning and we proceed on the current HEAD
    rather than blocking the whole conversation.
    """
    if project_root is None:
        return None
    if not (project_root / ".git").exists():
        return None

    cache_key = _thread_cache_key(
        bot_name=bot_name, chat_id=chat_id, thread_id=thread_id
    )
    now = now_fn()

    with _LOCK:
        entry = _CACHE.get(cache_key)
        if entry is not None and (now - entry.ts) < _CACHE_TTL_SECONDS:
            # Return a copy with synced_this_turn=False so callers can
            # tell this was a cache hit (e.g. to suppress user-visible
            # "🔄 已同步" updates on follow-up messages in the thread).
            cached = entry.snapshot
            return PreflightSnapshot(
                project_id=cached.project_id,
                branch=cached.branch,
                head_sha=cached.head_sha,
                head_sha_short=cached.head_sha_short,
                last_commit_subject=cached.last_commit_subject,
                last_commit_author=cached.last_commit_author,
                last_commit_relative=cached.last_commit_relative,
                sync_status=cached.sync_status,
                skip_reason=cached.skip_reason,
                pulled_commits=list(cached.pulled_commits),
                synced_this_turn=False,
                base_branch_requested=cached.base_branch_requested,
                base_branch_status=cached.base_branch_status,
                pending_force_sync_trace_id=cached.pending_force_sync_trace_id,
            )

    # Serialize the checkout + fetch/ff block across every bot
    # process pointing at this shared-repo. Without this, two role
    # bots receiving concurrent messages race on ``.git/index.lock``
    # and HEAD flaps between branches. See ``_repo_filelock`` for
    # the layered POSIX/in-proc locking strategy.
    with _repo_filelock(project_root):
        base_branch_status: str | None = None
        if base_branch:
            base_branch_status = _ensure_base_branch(
                project_root,
                base_branch=base_branch,
                thread_update_fn=thread_update_fn,
            )
            # Only a real HEAD mutation invalidates the snapshot cache.
            # Silent ``already_on`` stays cached; skip states leave HEAD
            # untouched, so their cached snapshots remain truthful.
            if base_branch_status == "switched":
                _invalidate_cache_for_root(project_root)

        sync_status = "sync_unavailable"
        skip_reason: str | None = None
        pulled_commits: list[str] = []
        pending_force_sync_trace_id: str | None = None

        if git_ops_service is not None and project_id:
            try:
                result = git_ops_service.sync_with_remote(project_id=project_id)
                sync_status = result.status
                pulled_commits = list(result.pulled_commits)
                if thread_update_fn is not None:
                    _fire_sync_update(thread_update_fn, result)
            except GitSyncDirtyError as exc:
                sync_status = "sync_skipped"
                skip_reason = f"worktree dirty: {str(exc)[:120]}"
                logger.warning("preflight sync skipped (dirty): %s", exc)
                _safe_thread_update(
                    thread_update_fn,
                    "⚠️ 未同步远端：工作区有未提交改动，请先处理本地变更",
                )
            except GitSyncDivergedError as exc:
                sync_status = "sync_skipped"
                skip_reason = f"branch diverged: {str(exc)[:120]}"
                logger.warning("preflight sync skipped (diverged): %s", exc)
                pending_force_sync_trace_id = _enqueue_force_sync_pending(
                    pending_action_service=pending_action_service,
                    chat_id=chat_id,
                    bot_name=bot_name,
                    project_id=project_id,
                    project_root=project_root,
                    exc=exc,
                    thread_update_fn=thread_update_fn,
                    target_branch=force_sync_target_branch,
                    remote=force_sync_remote,
                )
            except GitNoUpstreamError as exc:
                sync_status = "sync_skipped"
                skip_reason = "no upstream tracking branch"
                logger.info("preflight sync skipped (no upstream): %s", exc)
            except GitOpsError as exc:
                sync_status = "sync_skipped"
                skip_reason = f"git ops error: {str(exc)[:120]}"
                logger.warning("preflight sync failed: %s", exc)
            except Exception as exc:  # pragma: no cover - defensive
                sync_status = "sync_skipped"
                skip_reason = f"unexpected: {str(exc)[:120]}"
                logger.warning(
                    "preflight sync unexpected failure", exc_info=True
                )

        head_info = _capture_head_info(project_root)
        if head_info is None:
            return None

        snapshot = PreflightSnapshot(
            project_id=project_id or "",
            branch=head_info["branch"],
            head_sha=head_info["sha"],
            head_sha_short=head_info["sha"][:12] if head_info["sha"] else "",
            last_commit_subject=head_info["subject"],
            last_commit_author=head_info["author"],
            last_commit_relative=head_info["relative"],
            sync_status=sync_status,
            skip_reason=skip_reason,
            pulled_commits=pulled_commits,
            synced_this_turn=True,
            base_branch_requested=base_branch,
            base_branch_status=base_branch_status,
            pending_force_sync_trace_id=pending_force_sync_trace_id,
        )

        with _LOCK:
            _CACHE[cache_key] = _CacheEntry(snapshot=snapshot, ts=now)
            _prune_cache_locked(now)

        return snapshot


def render_baseline_for_prompt(snapshot: PreflightSnapshot) -> str:
    """Render the baseline block injected into the LLM system prompt.

    Kept tight on tokens: branch + short SHA + last commit subject +
    one-line sync status + a reminder that the baseline is a
    start-of-session snapshot (so the LLM doesn't report stale SHAs
    back to the user if later tools advance HEAD).
    """
    lines = ["## 仓库基线（会话启动时自动捕获）"]
    if snapshot.project_id:
        lines.append(f"- 项目：`{snapshot.project_id}`")
    lines.append(f"- 分支：`{snapshot.branch}`")
    head_line = f"- HEAD：`{snapshot.head_sha_short}`"
    if snapshot.last_commit_subject:
        head_line += f" — {snapshot.last_commit_subject}"
    meta_bits = [
        bit
        for bit in (
            snapshot.last_commit_author,
            snapshot.last_commit_relative,
        )
        if bit
    ]
    if meta_bits:
        head_line += "（" + "，".join(meta_bits) + "）"
    lines.append(head_line)

    # Role-baseline realignment status. Silent for the TL path
    # (``base_branch_requested is None``) and silent on the happy
    # "already_on" / "switched" paths (the branch line above already
    # tells the truth). Only the ``*_skip`` variants need an
    # explicit line, so the LLM knows the ``branch`` above is NOT
    # the baseline the caller asked for.
    if (
        snapshot.base_branch_requested
        and snapshot.base_branch_status in _BASE_BRANCH_SKIP_STATES
    ):
        want = snapshot.base_branch_requested
        reason_map = {
            "dirty_skip": f"工作区有未提交改动，未切到基线 `{want}`",
            "missing_skip": f"本地不存在基线分支 `{want}`",
            "error_skip": f"切到基线 `{want}` 失败（见运行日志）",
        }
        reason = reason_map.get(
            snapshot.base_branch_status or "",
            f"基线 `{want}` 未就绪",
        )
        lines.append(f"- 基线对齐：⚠️ {reason}；当前仍在 `{snapshot.branch}`")
        lines.append(
            "  - 如果本次任务依赖基线分支的内容，先告知用户处理未提交改动"
            "或补齐分支，再继续。"
        )

    # When ``synced_this_turn`` is False we're rendering a cache hit,
    # so we must avoid telling the LLM "just fetched" — the actual
    # fetch happened up to 5 minutes earlier. The difference is only
    # visible to the LLM but it materially changes how it reports
    # freshness back to the user.
    sync_suffix = (
        "（会话启动时已 fetch）"
        if snapshot.synced_this_turn
        else "（会话内缓存，未重新 fetch）"
    )
    if snapshot.sync_status == "up_to_date":
        lines.append(f"- 同步状态：✅ 已与远端一致{sync_suffix}")
    elif snapshot.sync_status == "fast_forwarded":
        pulled_n = len(snapshot.pulled_commits)
        lines.append(
            f"- 同步状态：✅ 本次启动自动拉取了 {pulled_n} 条新提交"
            if snapshot.synced_this_turn
            else f"- 同步状态：✅ 会话启动时拉取了 {pulled_n} 条新提交（当前为会话内缓存）"
        )
        if snapshot.pulled_commits:
            preview = "\n".join(
                f"  - {line}" for line in snapshot.pulled_commits[:5]
            )
            lines.append(preview)
            if pulled_n > 5:
                lines.append(f"  - …（还有 {pulled_n - 5} 条，略）")
    elif snapshot.sync_status == "ahead_no_action":
        lines.append("- 同步状态：ℹ️ 本地领先远端，未做改动")
    elif snapshot.sync_status == "sync_skipped":
        reason = snapshot.skip_reason or "unknown"
        lines.append(f"- 同步状态：⚠️ 已跳过（{reason}）")
        if snapshot.pending_force_sync_trace_id:
            # Divergence detected and we've already asked the user to
            # confirm a force-sync. Don't let the LLM re-prompt them or
            # claim the baseline is current — the confirm round-trip is
            # the only path forward here.
            lines.append(
                "  - 已向用户发起「硬重置到远端」确认提示（trace="
                f"{snapshot.pending_force_sync_trace_id}）；"
                "等待用户回复「确认」或「取消」。在此之前不要再重复催促，"
                "也不要伪装成已经同步。"
            )
        else:
            lines.append(
                "  - 这意味着 shared-repo 可能落后于 GitHub；在依赖最新 spec/artefact 前"
                "先把阻塞原因告诉用户，由人工决定下一步。"
            )
    elif snapshot.sync_status == "sync_unavailable":
        lines.append("- 同步状态：⏸ 未启用（当前项目无 git-ops 配置）")
    else:
        lines.append(f"- 同步状态：{snapshot.sync_status}")

    lines.append(
        "提醒：以上是会话开始时的基线。若随后调用工具改动了仓库（如 git_commit / "
        "git_push），实际 HEAD 可能已前进，以工具返回的最新 SHA 为准。"
        "另：用户本地工作区的未推送改动 agent 无法感知，如有异常先请用户推送或口头描述。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_thread_update(
    thread_update_fn: ThreadUpdateCallback | None, message: str
) -> None:
    if thread_update_fn is None:
        return
    try:
        thread_update_fn(message)
    except Exception:  # pragma: no cover - defensive
        logger.debug("preflight thread update failed", exc_info=True)


_DIVERGED_COUNTS_RE = re.compile(
    r"ahead\s+(?P<ahead>\d+),\s*behind\s+(?P<behind>\d+)"
)


def _parse_diverged_counts(message: str) -> tuple[int, int]:
    """Extract (ahead, behind) from ``GitSyncDivergedError.message``.

    ``GitOpsService.sync_with_remote`` formats the message as
    ``... (ahead N, behind M). ...``. We parse it rather than plumb
    the raw counts through an extra exception field because the error
    type is already part of a public cross-module contract. Returns
    ``(0, 0)`` if the regex misses — callers should render "未知" /
    "unknown" for the count rather than lying.
    """
    m = _DIVERGED_COUNTS_RE.search(message or "")
    if not m:
        return 0, 0
    try:
        return int(m.group("ahead")), int(m.group("behind"))
    except (TypeError, ValueError):
        return 0, 0


def _enqueue_force_sync_pending(
    *,
    pending_action_service: PendingActionService | None,
    chat_id: str | None,
    bot_name: str,
    project_id: str,
    project_root: Path,
    exc: GitSyncDivergedError,
    thread_update_fn: ThreadUpdateCallback | None,
    target_branch: str,
    remote: str,
) -> str | None:
    """On divergence, write a ``force_sync_to_remote`` pending action
    and emit a confirm-prompt on the Feishu thread.

    Returns the pending ``trace_id`` on success, ``None`` when we
    couldn't create the pending (service not wired, no ``chat_id``,
    or save failed). When ``None`` is returned we still post the
    legacy "需要人工 rebase/merge" warning so the human sees
    *something* in the thread — better to fall back to the old
    behavior than to go silent.
    """
    ahead, behind = _parse_diverged_counts(str(exc))
    current_branch = _safe_current_branch(project_root)

    if pending_action_service is None or not chat_id:
        _safe_thread_update(
            thread_update_fn,
            "⚠️ 未同步远端：本地与远端分叉，需要人工 rebase/merge",
        )
        return None

    from uuid import uuid4

    trace_id = f"pending-{uuid4().hex[:12]}"
    action = PendingAction(
        trace_id=trace_id,
        chat_id=chat_id,
        role_name=bot_name or "",
        action_type="force_sync_to_remote",
        action_args={
            "project_id": project_id,
            "remote": remote,
            "target_branch": target_branch,
            "ahead": ahead,
            "behind": behind,
            "current_branch": current_branch,
        },
    )
    try:
        pending_action_service.save(action)
    except Exception:  # pragma: no cover - defensive
        logger.warning(
            "failed to persist force_sync pending action", exc_info=True
        )
        _safe_thread_update(
            thread_update_fn,
            "⚠️ 未同步远端：本地与远端分叉，需要人工 rebase/merge",
        )
        return None

    ahead_bit = f"ahead {ahead}" if ahead else "ahead 未知"
    behind_bit = f"behind {behind}" if behind else "behind 未知"
    drop_bit = (
        f"将丢弃本地 {ahead} 条分叉提交"
        if ahead
        else "将丢弃所有本地分叉提交"
    )
    _safe_thread_update(
        thread_update_fn,
        (
            f"⚠️ 未同步远端：本地 `{current_branch or '当前分支'}` 与 "
            f"`{remote}/{target_branch}` 已分叉（{ahead_bit}, {behind_bit}）。\n"
            f"回复「确认」执行硬重置到 `{remote}/{target_branch}`"
            f"（{drop_bit}、未提交改动与未跟踪文件），或「取消」保持现状。"
        ),
    )
    return trace_id


def _safe_current_branch(root: Path) -> str:
    """Best-effort ``git rev-parse --abbrev-ref HEAD``. Returns empty
    string on any failure; callers must handle that.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "").strip()


def _fire_sync_update(
    thread_update_fn: ThreadUpdateCallback, result: GitSyncResult
) -> None:
    if result.status == "up_to_date":
        msg = (
            f"🔄 git 已同步：`{result.branch}` 已是最新"
            f"（{result.new_head_sha[:8]}）"
        )
    elif result.status == "fast_forwarded":
        msg = (
            f"🔄 git 已同步：`{result.branch}` 拉取 "
            f"{len(result.pulled_commits)} 条新提交"
            f"（{result.old_head_sha[:8]} → {result.new_head_sha[:8]}）"
        )
    elif result.status == "ahead_no_action":
        msg = (
            f"🔄 git 检查：`{result.branch}` 本地领先远端 "
            f"{result.ahead_count} 条（未改动）"
        )
    else:
        msg = f"🔄 git 同步：{result.status}"
    _safe_thread_update(thread_update_fn, msg)


def _ensure_base_branch(
    root: Path,
    *,
    base_branch: str,
    thread_update_fn: ThreadUpdateCallback | None,
) -> str:
    """Best-effort: switch the worktree to ``base_branch`` before sync.

    Why this exists: the shared-repo on the server is a single clone
    that several role bots point at. Whichever role last ran a
    workflow leaves HEAD on its branch. For roles whose baseline is
    the stable trunk (PM reading specs, researcher scanning briefs),
    inheriting that stale feature branch gives the LLM the wrong
    picture and causes freshly written artifacts to land on the
    wrong branch.

    Guard rails:
    - Already on ``base_branch`` → no-op, silent.
    - Worktree dirty → emit a ⚠️ warning and **stay on the current
      branch**. Forcing a checkout here would either error out or
      drag uncommitted edits onto main, both worse than a visible
      warning.
    - Target branch missing locally → emit ⚠️ warning and continue.
      We deliberately do not create it from ``origin/<base_branch>``
      here; that's a repo-setup concern, not something to do
      implicitly on every inbound message.
    - Any subprocess error → logged, treated like "stay put".

    Returns one of the ``base_branch_status`` literals (see the
    ``_BASE_BRANCH_SKIP_STATES`` set above): ``"already_on"`` (silent
    no-op), ``"switched"`` (HEAD moved), or one of the three
    ``*_skip`` variants on failure paths. The caller uses the return
    value for two things:
      1. stamp the outcome onto ``PreflightSnapshot.base_branch_status``
         so the rendered prompt can tell the LLM when realignment was
         denied;
      2. only invalidate the process-wide cache when the state is
         ``"switched"`` (the only path that mutated HEAD).

    We do NOT cache the result: the caller caches the downstream
    ``PreflightSnapshot``, so this only runs once per (bot, chat,
    thread) anyway.
    """
    try:
        current_proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        logger.warning(
            "ensure_base_branch: could not read current branch",
            exc_info=True,
        )
        return "error_skip"

    current = (current_proc.stdout or "").strip()
    if not current:
        return "error_skip"
    if current == base_branch:
        return "already_on"

    try:
        porcelain_proc = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        logger.warning(
            "ensure_base_branch: status probe failed; skipping switch",
            exc_info=True,
        )
        return "error_skip"

    if (porcelain_proc.stdout or "").strip():
        _safe_thread_update(
            thread_update_fn,
            f"⚠️ 未切到基线分支 `{base_branch}`：当前 `{current}` 有未提交改动，"
            "保留当前分支。",
        )
        logger.info(
            "ensure_base_branch: worktree dirty on %s, staying put "
            "(wanted=%s)",
            current,
            base_branch,
        )
        return "dirty_skip"

    # Verify the target branch exists locally. We don't auto-create
    # it from origin/<base_branch> — that'd hide bootstrap mistakes.
    verify_proc = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{base_branch}"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if verify_proc.returncode != 0:
        _safe_thread_update(
            thread_update_fn,
            f"⚠️ 未切到基线分支 `{base_branch}`：本地不存在该分支，"
            f"保留当前分支 `{current}`。",
        )
        logger.info(
            "ensure_base_branch: local branch %s missing, staying on %s",
            base_branch,
            current,
        )
        return "missing_skip"

    checkout_proc = subprocess.run(
        ["git", "checkout", base_branch],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if checkout_proc.returncode != 0:
        stderr_tail = (checkout_proc.stderr or "").strip().splitlines()[-1:]
        reason = stderr_tail[0] if stderr_tail else "unknown"
        _safe_thread_update(
            thread_update_fn,
            f"⚠️ 切到基线分支 `{base_branch}` 失败：{reason[:120]}，"
            f"保留 `{current}`。",
        )
        logger.warning(
            "ensure_base_branch: checkout %s failed: %s",
            base_branch,
            checkout_proc.stderr,
        )
        return "error_skip"

    _safe_thread_update(
        thread_update_fn,
        f"↩️ 已切回基线分支 `{base_branch}`（原 `{current}`）",
    )
    logger.info(
        "ensure_base_branch: switched %s -> %s",
        current,
        base_branch,
    )
    return "switched"


_HEAD_FORMAT_SEP = "\x1f"
_HEAD_LOG_FORMAT = f"%s{_HEAD_FORMAT_SEP}%an{_HEAD_FORMAT_SEP}%cr"


def _capture_head_info(root: Path) -> dict[str, str] | None:
    """Return ``{branch, sha, subject, author, relative}`` for HEAD.

    Uses a single ``log -1 --format`` call for the commit metadata so
    we're not paying three ``rev-parse`` round-trips per message.
    Returns ``None`` only on catastrophic failure (e.g. git binary
    missing) — a detached HEAD or missing upstream doesn't prevent
    baseline capture.
    """
    try:
        branch_proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        sha_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        log_proc = subprocess.run(
            ["git", "log", "-1", f"--format={_HEAD_LOG_FORMAT}"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except FileNotFoundError:
        logger.warning("git binary not found; preflight baseline unavailable")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("preflight baseline capture timed out")
        return None

    branch = (branch_proc.stdout or "").strip() or "HEAD"
    sha = (sha_proc.stdout or "").strip()
    log_line = (log_proc.stdout or "").strip()
    parts = log_line.split(_HEAD_FORMAT_SEP, 2) if log_line else []
    subject = parts[0] if len(parts) > 0 else ""
    author = parts[1] if len(parts) > 1 else ""
    relative = parts[2] if len(parts) > 2 else ""
    return {
        "branch": branch,
        "sha": sha,
        "subject": subject,
        "author": author,
        "relative": relative,
    }
