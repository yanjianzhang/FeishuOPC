"""Unit tests for the git-sync preflight helper.

We reuse the same bare-remote + clone harness that
``test_git_ops_service`` uses (git config-free, POSIX-only paths
inside ``tmp_path``). Each test exercises one branch of
``run_preflight_sync`` and asserts:

- The returned ``PreflightSnapshot`` captures the right ``sync_status``.
- ``render_baseline_for_prompt`` produces the expected prefix the
  LLM will see.
- The per-thread cache short-circuits the second call.
- Typed GitOps errors (dirty / diverged / no-upstream) degrade to
  ``sync_skipped`` without raising, so the runtime never crashes
  because the shared-repo was in an awkward state.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from feishu_agent.team.pending_action_service import PendingActionService
from feishu_agent.tools.code_write_service import (
    CodeWriteAuditLog,
    CodeWritePolicy,
)
from feishu_agent.tools.git_ops_service import GitOpsService
from feishu_agent.tools.git_sync_preflight import (
    PreflightSnapshot,
    render_baseline_for_prompt,
    reset_cache_for_tests,
    run_preflight_sync,
)
from feishu_agent.tools.pre_push_inspector import PrePushInspector

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not available"
)


def _git_env(home: Path) -> dict[str, str]:
    return {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
    }


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(cwd),
    )


@pytest.fixture(autouse=True)
def _clear_preflight_cache():
    reset_cache_for_tests()
    yield
    reset_cache_for_tests()


@pytest.fixture
def policy() -> CodeWritePolicy:
    return CodeWritePolicy(
        allowed_write_roots=("lib/", "test/"),
        require_confirmation_above_bytes=64 * 1024,
    )


@pytest.fixture
def repo_and_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "lib").mkdir()
    (work / "lib" / "initial.py").write_text("x = 1\n", encoding="utf-8")
    _git(work, "add", "lib/initial.py")
    _git(work, "commit", "-q", "-m", "initial: seed the repo")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-q", "origin", "main")
    _git(work, "checkout", "-q", "-b", "feature/work")
    return work, remote


@pytest.fixture
def services(
    repo_and_remote: tuple[Path, Path],
    policy: CodeWritePolicy,
    tmp_path: Path,
) -> tuple[GitOpsService, PrePushInspector, Path, Path]:
    work, remote = repo_and_remote
    inspector = PrePushInspector(
        project_roots={"proj": work},
        policies={"proj": policy},
    )
    audit = CodeWriteAuditLog(root=tmp_path / "audit", trace_id="t1")
    svc = GitOpsService(
        project_roots={"proj": work},
        policies={"proj": policy},
        inspector=inspector,
        audit_log=audit,
    )
    return svc, inspector, work, remote


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@requires_git
def test_preflight_up_to_date_captures_baseline_and_notifies(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
):
    svc, inspector, work, _ = services
    # Push once so the feature branch has an upstream.
    (work / "lib" / "feat.py").write_text("print('ok')\n", encoding="utf-8")
    svc.commit(project_id="proj", message="3-1: feat")
    report = inspector.inspect("proj")
    svc.push_current_branch(
        project_id="proj", inspection_token=report.inspection_token or ""
    )

    thread_updates: list[str] = []
    snap = run_preflight_sync(
        git_ops_service=svc,
        project_id="proj",
        project_root=work,
        bot_name="tech_lead",
        chat_id="chat-a",
        thread_id="thread-1",
        thread_update_fn=thread_updates.append,
    )
    assert snap is not None
    assert snap.sync_status == "up_to_date"
    assert snap.branch == "feature/work"
    assert snap.head_sha
    assert snap.head_sha_short == snap.head_sha[:12]
    assert snap.last_commit_subject.startswith("3-1:")
    assert snap.pulled_commits == []
    assert snap.synced_this_turn is True

    # One thread update for the sync, no warning lines.
    assert len(thread_updates) == 1
    assert thread_updates[0].startswith("🔄 git 已同步")

    rendered = render_baseline_for_prompt(snap)
    assert "仓库基线" in rendered
    assert snap.head_sha_short in rendered
    assert "feature/work" in rendered
    assert "✅" in rendered


@requires_git
def test_preflight_fast_forwards_and_lists_pulled_commits(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
    tmp_path: Path,
):
    svc, inspector, work, remote = services
    (work / "lib" / "feat.py").write_text("print('ok')\n", encoding="utf-8")
    svc.commit(project_id="proj", message="3-1: feat")
    report = inspector.inspect("proj")
    svc.push_current_branch(
        project_id="proj", inspection_token=report.inspection_token or ""
    )

    # Push an upstream commit via a sibling clone.
    work2 = tmp_path / "work2"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(work2)],
        check=True,
        capture_output=True,
    )
    _git(work2, "checkout", "-q", "feature/work")
    (work2 / "lib" / "upstream.py").write_text(
        "z = 9\n", encoding="utf-8"
    )
    _git(work2, "add", "lib/upstream.py")
    _git(work2, "commit", "-q", "-m", "upstream: new ff target")
    _git(work2, "push", "-q", "origin", "feature/work")

    thread_updates: list[str] = []
    snap = run_preflight_sync(
        git_ops_service=svc,
        project_id="proj",
        project_root=work,
        bot_name="tech_lead",
        chat_id="chat-ff",
        thread_id="thread-ff",
        thread_update_fn=thread_updates.append,
    )
    assert snap is not None
    assert snap.sync_status == "fast_forwarded"
    assert len(snap.pulled_commits) == 1
    assert "upstream" in snap.pulled_commits[0]
    rendered = render_baseline_for_prompt(snap)
    assert "本次启动自动拉取" in rendered


# ---------------------------------------------------------------------------
# Skip paths (dirty / diverged / no-upstream)
# ---------------------------------------------------------------------------


@requires_git
def test_preflight_skipped_when_no_upstream(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
):
    svc, _, work, _ = services
    thread_updates: list[str] = []
    snap = run_preflight_sync(
        git_ops_service=svc,
        project_id="proj",
        project_root=work,
        bot_name="tech_lead",
        chat_id="c",
        thread_id="t",
        thread_update_fn=thread_updates.append,
    )
    assert snap is not None
    assert snap.sync_status == "sync_skipped"
    assert snap.skip_reason == "no upstream tracking branch"
    # We deliberately don't spam the user on the no-upstream case —
    # first push of a new branch is normal, not a warning.
    assert thread_updates == []
    rendered = render_baseline_for_prompt(snap)
    assert "⚠️ 已跳过" in rendered


@requires_git
def test_preflight_skipped_when_worktree_dirty(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
    tmp_path: Path,
):
    svc, inspector, work, remote = services
    (work / "lib" / "feat.py").write_text("print('ok')\n", encoding="utf-8")
    svc.commit(project_id="proj", message="3-1: feat")
    report = inspector.inspect("proj")
    svc.push_current_branch(
        project_id="proj", inspection_token=report.inspection_token or ""
    )

    # Upstream advances.
    work2 = tmp_path / "work2"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(work2)],
        check=True,
        capture_output=True,
    )
    _git(work2, "checkout", "-q", "feature/work")
    (work2 / "lib" / "x.py").write_text("u=1\n", encoding="utf-8")
    _git(work2, "add", "lib/x.py")
    _git(work2, "commit", "-q", "-m", "upstream dirty probe")
    _git(work2, "push", "-q", "origin", "feature/work")

    # Local worktree is dirty — the FF should be refused.
    (work / "lib" / "feat.py").write_text(
        "print('dirty')\n", encoding="utf-8"
    )

    thread_updates: list[str] = []
    snap = run_preflight_sync(
        git_ops_service=svc,
        project_id="proj",
        project_root=work,
        bot_name="tech_lead",
        chat_id="c",
        thread_id="t",
        thread_update_fn=thread_updates.append,
    )
    assert snap is not None
    assert snap.sync_status == "sync_skipped"
    assert snap.skip_reason is not None and "dirty" in snap.skip_reason
    # User *does* see a warning for dirty / diverged because these
    # are actionable human states (someone has to resolve).
    assert any("⚠️" in u for u in thread_updates)


@requires_git
def test_preflight_skipped_when_diverged(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
    tmp_path: Path,
):
    """Local ahead AND behind upstream → GitSyncDivergedError path.

    Guards the defensive except-branch at tools/git_sync_preflight.py:204
    so a refactor that drops the diverged handler trips a regression
    instead of silently bubbling the error up to the runtime.
    """
    svc, inspector, work, remote = services
    (work / "lib" / "feat.py").write_text("print('ok')\n", encoding="utf-8")
    svc.commit(project_id="proj", message="3-1: feat")
    report = inspector.inspect("proj")
    svc.push_current_branch(
        project_id="proj", inspection_token=report.inspection_token or ""
    )

    work2 = tmp_path / "work2"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(work2)],
        check=True,
        capture_output=True,
    )
    _git(work2, "checkout", "-q", "feature/work")
    (work2 / "lib" / "upstream.py").write_text("u=1\n", encoding="utf-8")
    _git(work2, "add", "lib/upstream.py")
    _git(work2, "commit", "-q", "-m", "upstream extra")
    _git(work2, "push", "-q", "origin", "feature/work")

    (work / "lib" / "local.py").write_text("l=1\n", encoding="utf-8")
    svc.commit(project_id="proj", message="local extra")

    thread_updates: list[str] = []
    snap = run_preflight_sync(
        git_ops_service=svc,
        project_id="proj",
        project_root=work,
        bot_name="tech_lead",
        chat_id="c",
        thread_id="t",
        thread_update_fn=thread_updates.append,
    )
    assert snap is not None
    assert snap.sync_status == "sync_skipped"
    assert snap.skip_reason is not None and "diverged" in snap.skip_reason
    # Diverged is a human-actionable state — must produce a ⚠️ ping.
    assert any("⚠️" in u and "分叉" in u for u in thread_updates)
    # Baseline still captures local HEAD so the LLM isn't flying blind.
    assert snap.head_sha
    assert snap.branch == "feature/work"


# ---------------------------------------------------------------------------
# Caching & degradation
# ---------------------------------------------------------------------------


@requires_git
def test_preflight_cached_within_thread_short_circuits_sync(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
    monkeypatch,
):
    svc, inspector, work, _ = services
    (work / "lib" / "feat.py").write_text("print('ok')\n", encoding="utf-8")
    svc.commit(project_id="proj", message="3-1: feat")
    report = inspector.inspect("proj")
    svc.push_current_branch(
        project_id="proj", inspection_token=report.inspection_token or ""
    )

    calls: list[str] = []
    real_sync = svc.sync_with_remote

    def _counting_sync(*args, **kwargs):
        calls.append("sync")
        return real_sync(*args, **kwargs)

    monkeypatch.setattr(svc, "sync_with_remote", _counting_sync)

    first = run_preflight_sync(
        git_ops_service=svc,
        project_id="proj",
        project_root=work,
        bot_name="tech_lead",
        chat_id="chat-cache",
        thread_id="thread-cache",
    )
    second = run_preflight_sync(
        git_ops_service=svc,
        project_id="proj",
        project_root=work,
        bot_name="tech_lead",
        chat_id="chat-cache",
        thread_id="thread-cache",
    )
    assert isinstance(first, PreflightSnapshot)
    assert isinstance(second, PreflightSnapshot)
    # Only the first call actually fetched — the second returned the
    # cached snapshot with ``synced_this_turn=False``.
    assert calls == ["sync"]
    assert first.synced_this_turn is True
    assert second.synced_this_turn is False
    assert first.head_sha == second.head_sha

    # Rendered baseline must distinguish "just fetched" vs "cache hit"
    # so the LLM doesn't falsely claim freshness on follow-up turns.
    first_rendered = render_baseline_for_prompt(first)
    second_rendered = render_baseline_for_prompt(second)
    assert "会话启动时已 fetch" in first_rendered
    assert "会话内缓存" in second_rendered


@requires_git
def test_preflight_returns_none_when_project_root_missing(tmp_path: Path):
    # Not a git repo.
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    assert (
        run_preflight_sync(
            git_ops_service=None,
            project_id="proj",
            project_root=not_a_repo,
            bot_name="tech_lead",
            chat_id="c",
            thread_id="t",
        )
        is None
    )


@requires_git
def test_preflight_sync_unavailable_without_service_still_captures_head(
    repo_and_remote: tuple[Path, Path],
):
    work, _ = repo_and_remote
    snap = run_preflight_sync(
        git_ops_service=None,
        project_id="proj",
        project_root=work,
        bot_name="tech_lead",
        chat_id="c",
        thread_id="t",
    )
    assert snap is not None
    assert snap.sync_status == "sync_unavailable"
    assert snap.head_sha  # HEAD still captured so LLM sees a baseline
    rendered = render_baseline_for_prompt(snap)
    assert "未启用" in rendered


# ---------------------------------------------------------------------------
# base_branch — role-specific baseline realignment
#
# These tests cover the "PM always starts from main" behavior. The
# shared-repo clone on the server is shared across roles, so whatever
# branch the TL last worked on leaks into the PM's view unless the
# preflight helper explicitly pins the PM to the product trunk.
# ---------------------------------------------------------------------------


@requires_git
def test_preflight_base_branch_switches_when_clean(
    repo_and_remote: tuple[Path, Path],
):
    """Clean worktree on a feature branch → switch to base_branch first."""
    work, _ = repo_and_remote
    # Fixture leaves HEAD on ``feature/work`` — exactly the "TL left it
    # here" situation the PM suffers from.
    head_before = _git(work, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head_before == "feature/work"

    thread_updates: list[str] = []
    snap = run_preflight_sync(
        git_ops_service=None,  # skip the fetch path; we're testing the switch
        project_id="proj",
        project_root=work,
        bot_name="product_manager",
        chat_id="pm-chat",
        thread_id="pm-thread-a",
        thread_update_fn=thread_updates.append,
        base_branch="main",
    )
    assert snap is not None
    assert snap.branch == "main", "baseline should be captured AFTER the switch"
    assert any("↩️" in u and "main" in u for u in thread_updates), thread_updates

    # And the worktree itself actually moved — this is what keeps any
    # follow-up artifact write from landing on the feature branch.
    head_after = _git(work, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head_after == "main"


@requires_git
def test_preflight_base_branch_noop_when_already_on_it(
    repo_and_remote: tuple[Path, Path],
):
    work, _ = repo_and_remote
    _git(work, "checkout", "-q", "main")

    thread_updates: list[str] = []
    snap = run_preflight_sync(
        git_ops_service=None,
        project_id="proj",
        project_root=work,
        bot_name="product_manager",
        chat_id="pm-chat",
        thread_id="pm-thread-b",
        thread_update_fn=thread_updates.append,
        base_branch="main",
    )
    assert snap is not None
    assert snap.branch == "main"
    # Silent no-op: no "↩️" thread update, no spurious warning.
    assert not any("↩️" in u for u in thread_updates), thread_updates
    assert not any("⚠️" in u and "基线分支" in u for u in thread_updates)


@requires_git
def test_preflight_base_branch_warns_on_dirty_and_stays_put(
    repo_and_remote: tuple[Path, Path],
):
    """Dirty worktree → warn, don't force-switch, don't drag edits to main."""
    work, _ = repo_and_remote
    # Dirty the feature branch with an uncommitted change.
    (work / "lib" / "scratch.py").write_text("x=2\n", encoding="utf-8")

    thread_updates: list[str] = []
    snap = run_preflight_sync(
        git_ops_service=None,
        project_id="proj",
        project_root=work,
        bot_name="product_manager",
        chat_id="pm-chat",
        thread_id="pm-thread-c",
        thread_update_fn=thread_updates.append,
        base_branch="main",
    )
    assert snap is not None
    assert snap.branch == "feature/work", "must stay on the dirty branch"
    # User-visible warning is required — this is how the PM learns
    # that its baseline got degraded and can fall back to asking TL
    # to clean up.
    assert any(
        "⚠️" in u and "基线分支" in u and "main" in u for u in thread_updates
    ), thread_updates

    # And the worktree is untouched.
    head_after = _git(work, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head_after == "feature/work"
    porcelain = _git(work, "status", "--porcelain=v1").stdout.strip()
    assert "scratch.py" in porcelain


@requires_git
def test_preflight_base_branch_warns_when_local_branch_missing(
    repo_and_remote: tuple[Path, Path],
):
    """Requested base_branch doesn't exist locally → warn, do nothing."""
    work, _ = repo_and_remote
    # ``develop`` doesn't exist in the fixture.
    thread_updates: list[str] = []
    snap = run_preflight_sync(
        git_ops_service=None,
        project_id="proj",
        project_root=work,
        bot_name="product_manager",
        chat_id="pm-chat",
        thread_id="pm-thread-d",
        thread_update_fn=thread_updates.append,
        base_branch="develop",
    )
    assert snap is not None
    assert snap.branch == "feature/work"
    assert any(
        "⚠️" in u and "develop" in u and "本地不存在" in u for u in thread_updates
    ), thread_updates


@requires_git
def test_preflight_without_base_branch_keeps_existing_behavior(
    repo_and_remote: tuple[Path, Path],
):
    """TL path regression guard — no base_branch ⇒ no switch, no warning."""
    work, _ = repo_and_remote
    thread_updates: list[str] = []
    snap = run_preflight_sync(
        git_ops_service=None,
        project_id="proj",
        project_root=work,
        bot_name="tech_lead",
        chat_id="tl-chat",
        thread_id="tl-thread",
        thread_update_fn=thread_updates.append,
        # base_branch intentionally omitted
    )
    assert snap is not None
    assert snap.branch == "feature/work"
    # Nothing branch-switching-related should have been emitted.
    assert not any("↩️" in u for u in thread_updates)
    assert not any("基线分支" in u for u in thread_updates)


# ---------------------------------------------------------------------------
# base_branch — hardening (cache coherence, integration with real sync,
# prompt rendering of skip states)
#
# These tests cover the adversarial review findings H-1 and M-1/M-2:
# the original 5 tests proved the helper mutates HEAD in the right
# direction, but did not prove that (a) real ``sync_with_remote`` runs
# against the post-switch branch, (b) a switch invalidates any TL-bot
# cached snapshot so the next TL message captures the new HEAD, and
# (c) the LLM prompt is told when realignment was denied.
# ---------------------------------------------------------------------------


@requires_git
def test_preflight_base_branch_integration_switch_then_real_ff_sync(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
    tmp_path: Path,
):
    """End-to-end: start on feature/work, request base_branch=main,
    let GitOpsService fetch+FF, assert the pull happened on ``main``
    (not on ``feature/work``).

    Guards against a regression where ``_ensure_base_branch`` runs
    after ``sync_with_remote`` (same class of bug that caused the
    original incident — sync on the wrong branch).
    """
    svc, _, work, remote = services
    # Fixture left HEAD on feature/work. Now push an upstream commit
    # to ``main`` via a sibling clone, so ``main`` has something to
    # fast-forward to.
    work2 = tmp_path / "work2-main"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(work2)],
        check=True,
        capture_output=True,
    )
    _git(work2, "checkout", "-q", "main")
    (work2 / "lib" / "trunk.py").write_text("m = 1\n", encoding="utf-8")
    _git(work2, "add", "lib/trunk.py")
    _git(work2, "commit", "-q", "-m", "main: trunk advances")
    _git(work2, "push", "-q", "origin", "main")

    # Local ``main`` needs an upstream before FF can work. The fixture
    # already did ``git push -q origin main``, so ``main`` tracks
    # ``origin/main`` — but only after we checkout+push, which the
    # fixture already did. Nothing to set up here.

    thread_updates: list[str] = []
    snap = run_preflight_sync(
        git_ops_service=svc,
        project_id="proj",
        project_root=work,
        bot_name="product_manager",
        chat_id="pm-int",
        thread_id="pm-int-thread",
        thread_update_fn=thread_updates.append,
        base_branch="main",
    )
    assert snap is not None
    # Post-switch branch.
    assert snap.branch == "main"
    # The fetch+FF ran on main and actually pulled the trunk commit.
    assert snap.sync_status == "fast_forwarded", snap
    assert len(snap.pulled_commits) == 1
    assert "trunk advances" in snap.pulled_commits[0]
    # Exactly one switch update + one sync update, in that order.
    switch_msgs = [m for m in thread_updates if "↩️" in m]
    sync_msgs = [m for m in thread_updates if m.startswith("🔄 git 已同步")]
    assert len(switch_msgs) == 1
    assert len(sync_msgs) == 1
    # Snapshot carries the realignment outcome so the prompt can
    # differentiate "baseline == requested" from the fall-through paths.
    assert snap.base_branch_requested == "main"
    assert snap.base_branch_status == "switched"


@requires_git
def test_preflight_base_branch_switch_invalidates_previous_cache(
    repo_and_remote: tuple[Path, Path],
):
    """H-1: a successful switch must flush cached snapshots so the
    next call (possibly from a different bot on the same shared-repo)
    re-captures HEAD and doesn't hand the LLM a stale branch.
    """
    work, _ = repo_and_remote
    # 1) TL message: cache a snapshot on feature/work. No switch
    #    requested, so the cache entry tells the world branch=feature/work.
    tl_snap_1 = run_preflight_sync(
        git_ops_service=None,
        project_id="proj",
        project_root=work,
        bot_name="tech_lead",
        chat_id="tl-chat",
        thread_id="tl-thread-x",
    )
    assert tl_snap_1 is not None
    assert tl_snap_1.branch == "feature/work"
    assert tl_snap_1.synced_this_turn is True

    # 2) PM message on a DIFFERENT thread: requests base_branch=main,
    #    actually mutates HEAD. This must evict TL's cache entry.
    pm_snap = run_preflight_sync(
        git_ops_service=None,
        project_id="proj",
        project_root=work,
        bot_name="product_manager",
        chat_id="pm-chat",
        thread_id="pm-thread-x",
        base_branch="main",
    )
    assert pm_snap is not None
    assert pm_snap.branch == "main"
    assert pm_snap.base_branch_status == "switched"

    # 3) TL replies on the same (originally cached) thread. Before the
    #    fix this returned the stale feature/work snapshot even though
    #    HEAD is now main. After the fix, the cache is empty and TL
    #    re-captures HEAD → sees main.
    tl_snap_2 = run_preflight_sync(
        git_ops_service=None,
        project_id="proj",
        project_root=work,
        bot_name="tech_lead",
        chat_id="tl-chat",
        thread_id="tl-thread-x",
    )
    assert tl_snap_2 is not None
    assert tl_snap_2.branch == "main"
    assert tl_snap_2.synced_this_turn is True, "stale cache not evicted"


@requires_git
def test_preflight_render_surfaces_base_branch_skip_state(
    repo_and_remote: tuple[Path, Path],
):
    """M-1: when realignment is denied (dirty / missing / error), the
    rendered prompt must tell the LLM that the ``branch`` field is NOT
    the requested baseline. Otherwise the LLM proceeds as if the
    switch succeeded and writes artifacts to the wrong branch.
    """
    work, _ = repo_and_remote
    (work / "lib" / "scratch.py").write_text("x=2\n", encoding="utf-8")
    snap = run_preflight_sync(
        git_ops_service=None,
        project_id="proj",
        project_root=work,
        bot_name="product_manager",
        chat_id="pm-chat",
        thread_id="pm-thread-skip",
        base_branch="main",
    )
    assert snap is not None
    assert snap.base_branch_status == "dirty_skip"
    rendered = render_baseline_for_prompt(snap)
    # Warning line must reference the requested baseline and the
    # actual branch, so the LLM can't confuse them.
    assert "基线对齐" in rendered
    assert "main" in rendered
    assert "feature/work" in rendered
    assert "⚠️" in rendered


# ---------------------------------------------------------------------------
# Force-sync pending action on divergence
# ---------------------------------------------------------------------------


def _diverge_feature_work(
    svc: GitOpsService,
    inspector: PrePushInspector,
    work: Path,
    remote: Path,
    tmp_path: Path,
) -> None:
    """Set up local ahead 1 / behind 1 on ``feature/work`` so the next
    ``sync_with_remote`` trips ``GitSyncDivergedError``. Reused by the
    pending-action tests below.
    """
    (work / "lib" / "feat.py").write_text("print('ok')\n", encoding="utf-8")
    svc.commit(project_id="proj", message="seed-feat")
    report = inspector.inspect("proj")
    svc.push_current_branch(
        project_id="proj", inspection_token=report.inspection_token or ""
    )

    work2 = tmp_path / "work2-div"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(work2)],
        check=True,
        capture_output=True,
    )
    _git(work2, "checkout", "-q", "feature/work")
    (work2 / "lib" / "upstream.py").write_text("u=1\n", encoding="utf-8")
    _git(work2, "add", "lib/upstream.py")
    _git(work2, "commit", "-q", "-m", "upstream extra")
    _git(work2, "push", "-q", "origin", "feature/work")

    (work / "lib" / "local.py").write_text("l=1\n", encoding="utf-8")
    svc.commit(project_id="proj", message="local extra")


@requires_git
def test_preflight_diverged_enqueues_force_sync_pending_action(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
    tmp_path: Path,
):
    """When preflight trips divergence AND a PendingActionService is
    wired, we must (a) persist a ``force_sync_to_remote`` pending
    action keyed by chat_id, and (b) stamp its trace_id into the
    snapshot so the LLM knows the human is mid-confirm."""
    svc, inspector, work, remote = services
    _diverge_feature_work(svc, inspector, work, remote, tmp_path)

    pending_dir = tmp_path / "pending"
    pending_service = PendingActionService(pending_dir)

    thread_updates: list[str] = []
    snap = run_preflight_sync(
        git_ops_service=svc,
        project_id="proj",
        project_root=work,
        bot_name="tech_lead",
        chat_id="chat-diverged",
        thread_id="thread-div",
        thread_update_fn=thread_updates.append,
        pending_action_service=pending_service,
    )
    assert snap is not None
    assert snap.sync_status == "sync_skipped"
    assert snap.pending_force_sync_trace_id
    trace = snap.pending_force_sync_trace_id

    loaded = pending_service.load(trace)
    assert loaded is not None
    assert loaded.action_type == "force_sync_to_remote"
    assert loaded.chat_id == "chat-diverged"
    assert loaded.action_args["target_branch"] == "main"
    assert loaded.action_args["remote"] == "origin"
    assert loaded.action_args["project_id"] == "proj"
    assert loaded.action_args["ahead"] == 1
    assert loaded.action_args["behind"] == 1
    assert loaded.action_args["current_branch"] == "feature/work"

    # Thread update must prompt the human for 确认 / 取消; the
    # message is what they see in Feishu, so it's part of the
    # public contract.
    assert any(
        "⚠️" in m and "确认" in m and "取消" in m for m in thread_updates
    )


@requires_git
def test_preflight_diverged_falls_back_when_no_pending_service(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
    tmp_path: Path,
):
    """Without a PendingActionService (e.g. legacy caller), divergence
    still surfaces a ⚠️ warning and a snapshot — we just can't offer
    the confirm flow. Snapshot field stays ``None``."""
    svc, inspector, work, remote = services
    _diverge_feature_work(svc, inspector, work, remote, tmp_path)

    thread_updates: list[str] = []
    snap = run_preflight_sync(
        git_ops_service=svc,
        project_id="proj",
        project_root=work,
        bot_name="tech_lead",
        chat_id="chat-no-pending",
        thread_id="thread-nopending",
        thread_update_fn=thread_updates.append,
    )
    assert snap is not None
    assert snap.sync_status == "sync_skipped"
    assert snap.pending_force_sync_trace_id is None
    # Legacy warning text still used when we can't offer confirm.
    assert any(
        "⚠️" in m and "rebase/merge" in m for m in thread_updates
    )


@requires_git
def test_preflight_render_surfaces_pending_force_sync_trace(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
    tmp_path: Path,
):
    """When a pending force-sync exists, render_baseline_for_prompt
    must include a 'waiting for confirm' line so the LLM doesn't
    re-prompt or pretend we're synced."""
    svc, inspector, work, remote = services
    _diverge_feature_work(svc, inspector, work, remote, tmp_path)

    pending_service = PendingActionService(tmp_path / "pending-render")
    snap = run_preflight_sync(
        git_ops_service=svc,
        project_id="proj",
        project_root=work,
        bot_name="tech_lead",
        chat_id="chat-render",
        thread_id="thread-render",
        pending_action_service=pending_service,
    )
    assert snap is not None
    assert snap.pending_force_sync_trace_id

    rendered = render_baseline_for_prompt(snap)
    assert "硬重置" in rendered
    assert snap.pending_force_sync_trace_id in rendered
    # Must also tell the LLM not to re-prompt.
    assert "不要再重复" in rendered or "等待用户" in rendered
