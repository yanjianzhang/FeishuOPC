"""Unit tests for GitOpsService.

We spin up:
- a local bare remote in ``tmp_path/remote.git``
- a working clone on ``feature/work`` branch

and exercise commit + push through the service. Main/master is
protected, tokens are enforced, audit log gets appended.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from feishu_agent.tools.code_write_service import (
    CodeWriteAuditLog,
    CodeWritePolicy,
)
from feishu_agent.tools.git_ops_service import (
    GitBranchExistsError,
    GitBranchProtectedError,
    GitInspectionRequiredError,
    GitInvalidBranchSpecError,
    GitNothingToCommitError,
    GitNoUpstreamError,
    GitOpsError,
    GitOpsService,
    GitProjectError,
    GitSyncDirtyError,
    GitSyncDivergedError,
)
from feishu_agent.tools.pre_push_inspector import PrePushInspector

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not available"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    _git(work, "commit", "-q", "-m", "initial")
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
# Project resolution
# ---------------------------------------------------------------------------


def test_unknown_project(
    policy: CodeWritePolicy, tmp_path: Path
):
    inspector = PrePushInspector(project_roots={}, policies={})
    svc = GitOpsService(
        project_roots={},
        policies={},
        inspector=inspector,
    )
    with pytest.raises(GitProjectError):
        svc.commit(project_id="nope", message="hi")


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


@requires_git
def test_commit_refuses_on_protected_branch(
    services: tuple[GitOpsService, PrePushInspector, Path, Path]
):
    svc, _, work, _ = services
    _git(work, "checkout", "-q", "main")
    with pytest.raises(GitBranchProtectedError):
        svc.commit(project_id="proj", message="on main")


@requires_git
def test_commit_nothing_to_commit(
    services: tuple[GitOpsService, PrePushInspector, Path, Path]
):
    svc, _, _, _ = services
    with pytest.raises(GitNothingToCommitError):
        svc.commit(project_id="proj", message="empty")


@requires_git
def test_commit_happy_path_files_inside_policy(
    services: tuple[GitOpsService, PrePushInspector, Path, Path]
):
    svc, _, work, _ = services
    (work / "lib" / "feature.py").write_text("print('ok')\n", encoding="utf-8")
    (work / "lib" / "other.py").write_text("y = 2\n", encoding="utf-8")
    res = svc.commit(project_id="proj", message="3-1: add feature")
    assert res.branch == "feature/work"
    assert res.files_count == 2


@requires_git
def test_commit_skips_files_outside_policy(
    services: tuple[GitOpsService, PrePushInspector, Path, Path]
):
    """Files outside allowed_write_roots must not be staged by the
    service. If ONLY outside-policy files changed, it raises
    GitNothingToCommitError rather than making a bad commit."""
    svc, _, work, _ = services
    (work / "server").mkdir()
    (work / "server" / "main.py").write_text("print('srv')\n", encoding="utf-8")
    with pytest.raises(GitNothingToCommitError):
        svc.commit(project_id="proj", message="should refuse")


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


@requires_git
def test_push_refuses_without_token(
    services: tuple[GitOpsService, PrePushInspector, Path, Path]
):
    svc, _, work, _ = services
    (work / "lib" / "feature.py").write_text("print('ok')\n", encoding="utf-8")
    svc.commit(project_id="proj", message="3-1: feat")
    with pytest.raises(GitInspectionRequiredError):
        svc.push_current_branch(
            project_id="proj", inspection_token="garbage-token"
        )


@requires_git
def test_push_refuses_on_protected_branch(
    services: tuple[GitOpsService, PrePushInspector, Path, Path]
):
    svc, inspector, work, _ = services
    _git(work, "checkout", "-q", "main")
    report = inspector.inspect("proj")
    # Token was issued (inspection on clean main is ok), but push should
    # still refuse because the branch itself is protected.
    with pytest.raises(GitBranchProtectedError):
        svc.push_current_branch(
            project_id="proj",
            inspection_token=report.inspection_token or "",
        )


@requires_git
def test_push_happy_path_with_valid_token(
    services: tuple[GitOpsService, PrePushInspector, Path, Path]
):
    svc, inspector, work, remote = services
    (work / "lib" / "feature.py").write_text("print('ok')\n", encoding="utf-8")
    svc.commit(project_id="proj", message="3-1: feat")
    report = inspector.inspect("proj")
    assert report.ok
    assert report.inspection_token
    out = svc.push_current_branch(
        project_id="proj", inspection_token=report.inspection_token
    )
    assert out.branch == "feature/work"
    assert out.remote == "origin"
    # Remote should now have our branch.
    r = subprocess.run(
        ["git", "branch", "-a"],
        cwd=str(work),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "remotes/origin/feature/work" in r.stdout


@requires_git
def test_push_refuses_if_head_changed_after_inspection(
    services: tuple[GitOpsService, PrePushInspector, Path, Path]
):
    """If you commit *after* inspection, the token's pinned HEAD no
    longer matches and push must refuse."""
    svc, inspector, work, _ = services
    (work / "lib" / "feature.py").write_text("print('ok')\n", encoding="utf-8")
    svc.commit(project_id="proj", message="3-1: feat")
    report = inspector.inspect("proj")
    assert report.inspection_token
    # Now add another commit on top:
    (work / "lib" / "again.py").write_text("print('again')\n", encoding="utf-8")
    svc.commit(project_id="proj", message="3-1: part 2")
    with pytest.raises(GitInspectionRequiredError):
        svc.push_current_branch(
            project_id="proj",
            inspection_token=report.inspection_token,
        )


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@requires_git
def test_commit_and_push_write_audit_events(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
    tmp_path: Path,
):
    svc, inspector, work, _ = services
    (work / "lib" / "feature.py").write_text("print('ok')\n", encoding="utf-8")
    svc.commit(project_id="proj", message="3-1: feat")
    report = inspector.inspect("proj")
    assert report.inspection_token
    svc.push_current_branch(
        project_id="proj", inspection_token=report.inspection_token
    )
    audit_file = tmp_path / "audit" / "t1.jsonl"
    assert audit_file.is_file()
    lines = audit_file.read_text(encoding="utf-8").splitlines()
    events = [l for l in lines if '"event"' in l]
    assert any('"git_commit"' in e for e in events)
    assert any('"git_push"' in e for e in events)


# ---------------------------------------------------------------------------
# Sync with remote
# ---------------------------------------------------------------------------


@requires_git
def test_sync_up_to_date_after_push(
    services: tuple[GitOpsService, PrePushInspector, Path, Path]
):
    """Immediately after we push, remote tracks us: status=up_to_date."""
    svc, inspector, work, _ = services
    (work / "lib" / "feature.py").write_text("print('ok')\n", encoding="utf-8")
    svc.commit(project_id="proj", message="3-1: feat")
    report = inspector.inspect("proj")
    svc.push_current_branch(
        project_id="proj", inspection_token=report.inspection_token or ""
    )
    res = svc.sync_with_remote(project_id="proj")
    assert res.status == "up_to_date"
    assert res.ahead_count == 0
    assert res.behind_count == 0
    assert res.pulled_commits == []


@requires_git
def test_sync_no_upstream_for_new_branch(
    services: tuple[GitOpsService, PrePushInspector, Path, Path]
):
    """A never-pushed feature branch has no upstream yet."""
    svc, _, _, _ = services
    with pytest.raises(GitNoUpstreamError):
        svc.sync_with_remote(project_id="proj")


@requires_git
def test_sync_fast_forward_pulls_new_upstream_commit(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
    tmp_path: Path,
):
    """If the remote has a new commit and our worktree is clean, we
    fast-forward pull and report the pulled commits."""
    svc, inspector, work, remote = services
    # Push our feature branch once to establish upstream:
    (work / "lib" / "feature.py").write_text("print('ok')\n", encoding="utf-8")
    svc.commit(project_id="proj", message="3-1: feat")
    report = inspector.inspect("proj")
    svc.push_current_branch(
        project_id="proj", inspection_token=report.inspection_token or ""
    )

    # Make a second clone, push an extra commit upstream:
    work2 = tmp_path / "work2"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(work2)],
        check=True,
        capture_output=True,
        env=_git_env(work2 if work2.exists() else tmp_path),
    )
    work2.mkdir(exist_ok=True)
    _git(work2, "checkout", "-q", "feature/work")
    (work2 / "lib" / "second.py").write_text("print('two')\n", encoding="utf-8")
    _git(work2, "add", "lib/second.py")
    _git(work2, "commit", "-q", "-m", "3-1: from other clone")
    _git(work2, "push", "-q", "origin", "feature/work")

    # Now sync the primary clone — should FF pull the new commit:
    res = svc.sync_with_remote(project_id="proj")
    assert res.status == "fast_forwarded"
    assert res.behind_count == 1
    assert res.ahead_count == 0
    assert len(res.pulled_commits) == 1
    assert "from other clone" in res.pulled_commits[0]
    assert (work / "lib" / "second.py").is_file()


@requires_git
def test_sync_refuses_on_dirty_worktree(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
    tmp_path: Path,
):
    """If remote is ahead but worktree is dirty, sync must refuse to
    pull (risk of losing local edits under FF)."""
    svc, inspector, work, remote = services
    # Establish upstream:
    (work / "lib" / "feature.py").write_text("print('ok')\n", encoding="utf-8")
    svc.commit(project_id="proj", message="3-1: feat")
    report = inspector.inspect("proj")
    svc.push_current_branch(
        project_id="proj", inspection_token=report.inspection_token or ""
    )
    # Add an upstream commit via a second clone:
    work2 = tmp_path / "work2"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(work2)],
        check=True,
        capture_output=True,
    )
    _git(work2, "checkout", "-q", "feature/work")
    (work2 / "lib" / "up.py").write_text("x = 9\n", encoding="utf-8")
    _git(work2, "add", "lib/up.py")
    _git(work2, "commit", "-q", "-m", "upstream")
    _git(work2, "push", "-q", "origin", "feature/work")

    # Now dirty the primary worktree (uncommitted edit):
    (work / "lib" / "feature.py").write_text("print('dirty')\n", encoding="utf-8")
    with pytest.raises(GitSyncDirtyError):
        svc.sync_with_remote(project_id="proj")


@requires_git
def test_sync_refuses_on_diverged(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
    tmp_path: Path,
):
    """If local and upstream have commits the other doesn't, we must
    refuse to FF and let the human resolve."""
    svc, inspector, work, remote = services
    # Establish upstream:
    (work / "lib" / "feature.py").write_text("print('ok')\n", encoding="utf-8")
    svc.commit(project_id="proj", message="3-1: feat")
    report = inspector.inspect("proj")
    svc.push_current_branch(
        project_id="proj", inspection_token=report.inspection_token or ""
    )
    # Upstream advances:
    work2 = tmp_path / "work2"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(work2)],
        check=True,
        capture_output=True,
    )
    _git(work2, "checkout", "-q", "feature/work")
    (work2 / "lib" / "upstream.py").write_text("x = 'u'\n", encoding="utf-8")
    _git(work2, "add", "lib/upstream.py")
    _git(work2, "commit", "-q", "-m", "upstream extra")
    _git(work2, "push", "-q", "origin", "feature/work")

    # Local also advances independently:
    (work / "lib" / "local.py").write_text("y = 'l'\n", encoding="utf-8")
    svc.commit(project_id="proj", message="local extra")

    with pytest.raises(GitSyncDivergedError):
        svc.sync_with_remote(project_id="proj")


@requires_git
def test_sync_audit_records_fast_forward(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
    tmp_path: Path,
):
    """Successful FF must emit a git_sync audit line."""
    svc, inspector, work, remote = services
    (work / "lib" / "feature.py").write_text("print('ok')\n", encoding="utf-8")
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
    (work2 / "lib" / "second.py").write_text("z = 1\n", encoding="utf-8")
    _git(work2, "add", "lib/second.py")
    _git(work2, "commit", "-q", "-m", "second")
    _git(work2, "push", "-q", "origin", "feature/work")

    svc.sync_with_remote(project_id="proj")
    audit_file = tmp_path / "audit" / "t1.jsonl"
    assert audit_file.is_file()
    content = audit_file.read_text(encoding="utf-8")
    assert '"git_sync"' in content


# ---------------------------------------------------------------------------
# start_work_branch
# ---------------------------------------------------------------------------


@requires_git
def test_start_work_branch_happy_path_from_main(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
):
    """Creates ``feat/3-2-steal-api`` off ``origin/main`` while fixture
    leaves us on ``feature/work``. After the call HEAD must be on the
    new branch at origin/main's tip, not at feature/work's tip."""
    svc, _, work, _ = services
    # Ensure we're NOT on main — fixture leaves us on feature/work.
    branch_before = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(work), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch_before == "feature/work"

    res = svc.start_work_branch(
        project_id="proj",
        kind="feat",
        slug="3-2-steal-api",
    )

    assert res.branch == "feat/3-2-steal-api"
    assert res.base == "main"
    assert res.previous_branch == "feature/work"
    assert res.discarded_dirty_paths == []
    # HEAD must now be the new branch.
    branch_after = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(work), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch_after == "feat/3-2-steal-api"
    # New branch's HEAD must equal origin/main's tip (not feature/work's).
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(work), capture_output=True, text=True, check=True,
    ).stdout.strip()
    origin_main_sha = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=str(work), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head_sha == origin_main_sha == res.base_upstream_sha


@requires_git
def test_start_work_branch_rejects_invalid_kind(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
):
    svc, _, _, _ = services
    with pytest.raises(GitInvalidBranchSpecError):
        svc.start_work_branch(
            project_id="proj", kind="yolo", slug="ok"
        )


@requires_git
def test_start_work_branch_rejects_invalid_slug(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
):
    svc, _, _, _ = services
    # Uppercase + spaces + starts with dash — all invalid.
    for bad in ("FOO", "has space", "-leading-dash", ""):
        with pytest.raises(GitInvalidBranchSpecError):
            svc.start_work_branch(
                project_id="proj", kind="feat", slug=bad
            )


@requires_git
def test_start_work_branch_rejects_protected(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
    policy: CodeWritePolicy,
):
    """If slug ends up mapping to a protected branch name, refuse."""
    # Build a new service where 'feat/hot' is protected.
    work, remote = services[2], services[3]
    protected_policy = CodeWritePolicy(
        allowed_write_roots=policy.allowed_write_roots,
        require_confirmation_above_bytes=policy.require_confirmation_above_bytes,
        protected_branches=("main", "master", "feat/hot"),
    )
    inspector = PrePushInspector(
        project_roots={"proj": work},
        policies={"proj": protected_policy},
    )
    svc2 = GitOpsService(
        project_roots={"proj": work},
        policies={"proj": protected_policy},
        inspector=inspector,
    )
    with pytest.raises(GitBranchProtectedError):
        svc2.start_work_branch(project_id="proj", kind="feat", slug="hot")


@requires_git
def test_start_work_branch_rejects_existing_branch(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
):
    svc, _, work, _ = services
    _git(work, "branch", "feat/already-there")
    with pytest.raises(GitBranchExistsError):
        svc.start_work_branch(
            project_id="proj", kind="feat", slug="already-there"
        )


@requires_git
def test_start_work_branch_refuses_dirty_tree_by_default(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
):
    svc, _, work, _ = services
    (work / "lib" / "scratch.py").write_text("dirt = True\n", encoding="utf-8")
    with pytest.raises(GitSyncDirtyError):
        svc.start_work_branch(
            project_id="proj", kind="feat", slug="should-fail"
        )
    # New branch must NOT have been created after the refusal.
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/heads/feat/should-fail"],
        cwd=str(work), capture_output=True, text=True, check=False,
    )
    assert proc.returncode != 0


@requires_git
def test_start_work_branch_discards_dirty_with_opt_in(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
):
    svc, _, work, _ = services
    (work / "lib" / "scratch.py").write_text("dirt = True\n", encoding="utf-8")
    res = svc.start_work_branch(
        project_id="proj",
        kind="feat",
        slug="with-discard",
        allow_discard_dirty=True,
    )
    assert res.branch == "feat/with-discard"
    assert "lib/scratch.py" in res.discarded_dirty_paths
    # Dirty file must be gone after the discard.
    assert not (work / "lib" / "scratch.py").exists()


@requires_git
def test_start_work_branch_unpushed_commits_preserved_on_old_branch(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
):
    """Unpushed commits on ``feature/work`` must remain on that branch
    after we start a new branch — they live in git history, only the
    worktree switches."""
    svc, _, work, _ = services
    (work / "lib" / "local_feature.py").write_text(
        "local = 'only'\n", encoding="utf-8"
    )
    svc.commit(project_id="proj", message="3-1: local wip")
    feature_tip_before = subprocess.run(
        ["git", "rev-parse", "feature/work"],
        cwd=str(work), capture_output=True, text=True, check=True,
    ).stdout.strip()

    svc.start_work_branch(project_id="proj", kind="fix", slug="new-one")

    # feature/work still points at the unpushed commit.
    feature_tip_after = subprocess.run(
        ["git", "rev-parse", "feature/work"],
        cwd=str(work), capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert feature_tip_before == feature_tip_after
    # And the file still exists in that branch's tree.
    ls = subprocess.run(
        ["git", "ls-tree", "-r", "feature/work", "--name-only"],
        cwd=str(work), capture_output=True, text=True, check=True,
    ).stdout
    assert "lib/local_feature.py" in ls


@requires_git
def test_start_work_branch_unknown_base(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
):
    svc, _, _, _ = services
    with pytest.raises(GitNoUpstreamError):
        svc.start_work_branch(
            project_id="proj",
            kind="feat",
            slug="ok",
            base_branch="does-not-exist",
        )


@requires_git
def test_start_work_branch_audit_record(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
    tmp_path: Path,
):
    svc, _, _, _ = services
    svc.start_work_branch(project_id="proj", kind="feat", slug="audit-me")
    audit_file = tmp_path / "audit" / "t1.jsonl"
    assert audit_file.is_file()
    content = audit_file.read_text(encoding="utf-8")
    assert '"start_work_branch"' in content
    assert "feat/audit-me" in content


# ---------------------------------------------------------------------------
# force_sync_to_remote
# ---------------------------------------------------------------------------


def _diverge_main(
    work: Path, remote: Path, tmp_path: Path
) -> tuple[str, str]:
    """Create a diverged ``main``: local has 1 extra commit, origin/main
    has 1 different extra commit. Returns ``(local_divergent_sha,
    remote_tip_sha)`` before the force-sync is applied.

    Starts with the fixture invariant that ``main`` exists on both sides
    at the same SHA. We clone a second worktree, push an upstream-only
    commit on main, then commit a different change locally on main.
    """
    # upstream-only commit
    work2 = tmp_path / "work2_div"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(work2)],
        check=True,
        capture_output=True,
    )
    _git(work2, "checkout", "-q", "main")
    (work2 / "lib" / "upstream_only.py").write_text("u = 1\n", encoding="utf-8")
    _git(work2, "add", "lib/upstream_only.py")
    _git(work2, "commit", "-q", "-m", "upstream-only on main")
    _git(work2, "push", "-q", "origin", "main")
    remote_tip = _git(work2, "rev-parse", "HEAD").stdout.strip()

    # local-only divergent commit
    _git(work, "checkout", "-q", "main")
    (work / "lib" / "local_only.py").write_text("l = 1\n", encoding="utf-8")
    _git(work, "add", "lib/local_only.py")
    _git(work, "commit", "-q", "-m", "local-only on main")
    local_div = _git(work, "rev-parse", "HEAD").stdout.strip()
    return local_div, remote_tip


@requires_git
def test_force_sync_to_remote_resets_main_to_origin(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
    tmp_path: Path,
):
    svc, _, work, remote = services
    local_div, remote_tip = _diverge_main(work, remote, tmp_path)

    res = svc.force_sync_to_remote(project_id="proj")

    head_after = _git(work, "rev-parse", "HEAD").stdout.strip()
    assert head_after == remote_tip
    assert res.branch == "main"
    assert res.remote == "origin"
    assert res.new_head_sha == remote_tip
    assert res.previous_head_sha == local_div
    current_branch = _git(
        work, "rev-parse", "--abbrev-ref", "HEAD"
    ).stdout.strip()
    assert current_branch == "main"


@requires_git
def test_force_sync_keeps_local_commits_reachable_via_reflog(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
    tmp_path: Path,
):
    """Users told us the destructive UX is only safe if they can get
    their divergent commits back. We audit the SHA and rely on
    ``git reflog`` — prove the ref still exists after the reset."""
    svc, _, work, remote = services
    local_div, _ = _diverge_main(work, remote, tmp_path)

    svc.force_sync_to_remote(project_id="proj")

    reflog = _git(work, "reflog", "--pretty=%H").stdout
    assert local_div in reflog


@requires_git
def test_force_sync_cleans_untracked_files(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
    tmp_path: Path,
):
    svc, _, work, remote = services
    _diverge_main(work, remote, tmp_path)
    # Sprinkle untracked cruft at root + nested.
    (work / "scratch.log").write_text("junk\n", encoding="utf-8")
    (work / "tmp_dir").mkdir()
    (work / "tmp_dir" / "x.txt").write_text("y", encoding="utf-8")
    assert (work / "scratch.log").exists()
    assert (work / "tmp_dir").exists()

    res = svc.force_sync_to_remote(project_id="proj")

    assert not (work / "scratch.log").exists()
    assert not (work / "tmp_dir").exists()
    assert res.cleaned_paths_count >= 2


@requires_git
def test_force_sync_preserves_gitignored_files(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
    tmp_path: Path,
):
    """Without ``-x`` on git clean, ``.gitignore``-covered content must
    survive — blowing those away would wipe venv / node_modules /
    build caches and strand the user for no safety win.
    """
    svc, _, work, remote = services
    # The fixture leaves us on feature/work. Commit .gitignore onto
    # main (and push) so the force-sync target ref actually carries
    # the ignore rule when we later checkout -B main origin/main.
    _git(work, "checkout", "-q", "main")
    (work / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    _git(work, "add", ".gitignore")
    _git(work, "commit", "-q", "-m", "gitignore venv")
    _git(work, "push", "-q", "origin", "main")
    _diverge_main(work, remote, tmp_path)
    (work / ".venv").mkdir()
    (work / ".venv" / "dontdelete.txt").write_text("KEEP", encoding="utf-8")

    svc.force_sync_to_remote(project_id="proj")

    assert (work / ".venv" / "dontdelete.txt").exists()


@requires_git
def test_force_sync_from_feature_branch_switches_to_main(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
    tmp_path: Path,
):
    """User lands in a confirm from a feature branch. Regardless of
    where they were, force-sync must return them to a clean
    ``origin/main``."""
    svc, _, work, remote = services
    local_div, remote_tip = _diverge_main(work, remote, tmp_path)
    # Leave the worktree on the feature branch to prove force-sync
    # does its own checkout.
    _git(work, "checkout", "-q", "feature/work")

    res = svc.force_sync_to_remote(project_id="proj")

    current_branch = _git(
        work, "rev-parse", "--abbrev-ref", "HEAD"
    ).stdout.strip()
    assert current_branch == "main"
    assert res.previous_branch == "feature/work"
    assert res.new_head_sha == remote_tip


@requires_git
def test_force_sync_unknown_remote_branch(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
):
    svc, _, _, _ = services
    with pytest.raises(GitNoUpstreamError):
        svc.force_sync_to_remote(
            project_id="proj", target_branch="not-a-real-branch"
        )


@requires_git
def test_force_sync_audit_record(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
    tmp_path: Path,
):
    svc, _, work, remote = services
    local_div, remote_tip = _diverge_main(work, remote, tmp_path)

    svc.force_sync_to_remote(project_id="proj")

    audit_file = tmp_path / "audit" / "t1.jsonl"
    assert audit_file.is_file()
    content = audit_file.read_text(encoding="utf-8")
    assert '"force_sync_to_remote"' in content
    assert local_div in content  # previous_head_sha must be recoverable
    assert remote_tip in content


@requires_git
def test_force_sync_requires_known_project(
    policy: CodeWritePolicy,
):
    inspector = PrePushInspector(project_roots={}, policies={})
    svc = GitOpsService(
        project_roots={}, policies={}, inspector=inspector
    )
    with pytest.raises(GitProjectError):
        svc.force_sync_to_remote(project_id="nope")


@requires_git
def test_force_sync_rejects_empty_target_branch(
    services: tuple[GitOpsService, PrePushInspector, Path, Path],
):
    svc, _, _, _ = services
    with pytest.raises(GitOpsError):
        svc.force_sync_to_remote(project_id="proj", target_branch="")
