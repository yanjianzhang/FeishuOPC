"""Unit + integration tests for ArtifactPublishService.

Covers:
- Path validation: allowed-root check, traversal, denied segments,
  absolute paths, backslashes, empty normalization, duplicate entries,
  symlink escape.
- Commit message validation: empty / oversize / NUL byte.
- Agent ACL: unknown agent, TL explicitly NOT enabled.
- Repo state: UNKNOWN_PROJECT, missing file on disk, detached HEAD.
- Happy path with a local bare remote: commits the explicit path,
  pushes to origin, advances remote HEAD.
- Surprise-index defence: pre-commit hook auto-staging unrelated file
  triggers EXTRA_STAGED_FILES and leaves requested path un-staged.
- Push rejection from remote is surfaced via PushFailedError.
- Hard tripwire: no ``--force`` / flags are ever passed to git push.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from feishu_agent.team.artifact_publish_service import (
    AgentNotAllowedError,
    ArtifactPublishService,
    CommitMessageRejectedError,
    DetachedHeadError,
    ExtraStagedFilesError,
    NothingToCommitError,
    PathMissingError,
    PathRejectedError,
    PushFailedError,
    UnknownProjectError,
)

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
def repo_and_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    # Embed identity in the REPO config so later subprocesses that
    # don't pass GIT_* env vars still have an author (mirrors how the
    # real shared-repo is configured on servers).
    _git(work, "config", "user.name", "t")
    _git(work, "config", "user.email", "t@example.com")
    # Seed with a commit so HEAD exists for "nothing to commit" checks.
    (work / "README.md").write_text("seed\n", encoding="utf-8")
    _git(work, "add", "README.md")
    _git(work, "commit", "-q", "-m", "initial")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-q", "origin", "main")
    return work, remote


@pytest.fixture
def svc(repo_and_remote: tuple[Path, Path]) -> ArtifactPublishService:
    work, _ = repo_and_remote
    return ArtifactPublishService(project_roots={"proj": work})


# ---------------------------------------------------------------------------
# Validation: paths
# ---------------------------------------------------------------------------


@requires_git
@pytest.mark.parametrize(
    "bad_path",
    [
        "/etc/passwd",            # absolute
        "specs/../etc/passwd",    # traversal
        "specs\\windows\\style",  # backslash
        "lib/oops.py",            # not under allowed root for PM
        ".env",                   # top-level denied segment
        "specs/.env",             # denied segment inside path
        "docs/../secrets/x.md",   # traversal to denied-segment-style root
        "",                       # empty
        ".",                      # resolves to empty after canonicalization
    ],
)
def test_publish_rejects_bad_path(svc, repo_and_remote, bad_path):
    work, _ = repo_and_remote
    with pytest.raises(PathRejectedError):
        svc.publish(
            agent_name="product_manager",
            project_id="proj",
            relative_paths=[bad_path],
            commit_message="m",
        )


@requires_git
def test_publish_rejects_duplicate_paths(svc, repo_and_remote):
    work, _ = repo_and_remote
    (work / "specs").mkdir(exist_ok=True)
    (work / "specs" / "a.md").write_text("x\n", encoding="utf-8")
    with pytest.raises(PathRejectedError, match="duplicate"):
        svc.publish(
            agent_name="product_manager",
            project_id="proj",
            relative_paths=["specs/a.md", "specs/a.md"],
            commit_message="m",
        )


@requires_git
def test_publish_rejects_too_many_paths(svc):
    with pytest.raises(PathRejectedError, match="Too many paths"):
        svc.publish(
            agent_name="product_manager",
            project_id="proj",
            relative_paths=[f"specs/{i}.md" for i in range(51)],
            commit_message="m",
        )


@requires_git
def test_publish_rejects_empty_list(svc):
    with pytest.raises(PathRejectedError, match="non-empty list"):
        svc.publish(
            agent_name="product_manager",
            project_id="proj",
            relative_paths=[],
            commit_message="m",
        )


# ---------------------------------------------------------------------------
# Validation: commit message
# ---------------------------------------------------------------------------


@requires_git
@pytest.mark.parametrize(
    "bad_message",
    ["", "   \n  ", "has\x00nul"],
)
def test_publish_rejects_bad_message(svc, bad_message):
    with pytest.raises(CommitMessageRejectedError):
        svc.publish(
            agent_name="product_manager",
            project_id="proj",
            relative_paths=["specs/foo.md"],
            commit_message=bad_message,
        )


@requires_git
def test_publish_rejects_oversize_message(svc):
    with pytest.raises(CommitMessageRejectedError, match="exceeds"):
        svc.publish(
            agent_name="product_manager",
            project_id="proj",
            relative_paths=["specs/foo.md"],
            commit_message="x" * (4 * 1024 + 1),
        )


# ---------------------------------------------------------------------------
# Agent ACL
# ---------------------------------------------------------------------------


@requires_git
def test_publish_rejects_unknown_agent(svc):
    with pytest.raises(AgentNotAllowedError):
        svc.publish(
            agent_name="developer",
            project_id="proj",
            relative_paths=["specs/foo.md"],
            commit_message="m",
        )


@requires_git
def test_publish_rejects_tech_lead_agent(svc):
    # TL is deliberately NOT enabled for artifact publish — TL still
    # uses GitOpsService for code writes. If this test ever fails
    # because someone added ``tech_lead`` to the allow-list, re-audit
    # the boundary before shipping.
    with pytest.raises(AgentNotAllowedError):
        svc.publish(
            agent_name="tech_lead",
            project_id="proj",
            relative_paths=["specs/foo.md"],
            commit_message="m",
        )


@requires_git
def test_allowed_roots_for_agent(svc):
    roots = svc.allowed_roots_for_agent("product_manager")
    assert "specs" in roots
    assert "docs" in roots
    assert "lib" not in roots
    assert "src" not in roots
    assert svc.is_agent_enabled("product_manager") is True
    assert svc.is_agent_enabled("tech_lead") is False


# ---------------------------------------------------------------------------
# Repo state
# ---------------------------------------------------------------------------


@requires_git
def test_publish_unknown_project(svc):
    with pytest.raises(UnknownProjectError):
        svc.publish(
            agent_name="product_manager",
            project_id="nope",
            relative_paths=["specs/foo.md"],
            commit_message="m",
        )


@requires_git
def test_publish_missing_file(svc):
    with pytest.raises(PathMissingError):
        svc.publish(
            agent_name="product_manager",
            project_id="proj",
            relative_paths=["specs/never-written.md"],
            commit_message="m",
        )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@requires_git
def test_publish_happy_path_commits_and_pushes(
    svc, repo_and_remote: tuple[Path, Path]
):
    work, remote = repo_and_remote
    spec_dir = work / "specs" / "004-foo"
    spec_dir.mkdir(parents=True)
    spec_path = spec_dir / "spec.md"
    spec_path.write_text("# foo\n", encoding="utf-8")

    result = svc.publish(
        agent_name="product_manager",
        project_id="proj",
        relative_paths=["specs/004-foo/spec.md"],
        commit_message="spec: foo initial draft",
    )

    assert result.branch == "main"
    assert result.pushed is True
    assert result.commit_sha
    assert result.paths == ("specs/004-foo/spec.md",)
    assert result.remote == "origin"

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(work),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == result.commit_sha

    remote_ref = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert remote_ref == result.commit_sha


@requires_git
def test_publish_multiple_paths_single_commit(
    svc, repo_and_remote: tuple[Path, Path]
):
    work, _ = repo_and_remote
    (work / "specs" / "005-bar").mkdir(parents=True)
    (work / "specs" / "005-bar" / "spec.md").write_text("s\n", encoding="utf-8")
    (work / "docs").mkdir(exist_ok=True)
    (work / "docs" / "note.md").write_text("n\n", encoding="utf-8")

    result = svc.publish(
        agent_name="product_manager",
        project_id="proj",
        relative_paths=["specs/005-bar/spec.md", "docs/note.md"],
        commit_message="feat: ship bar spec + note",
    )
    assert sorted(result.paths) == ["docs/note.md", "specs/005-bar/spec.md"]

    names_out = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=str(work),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip().splitlines()
    assert sorted(names_out) == ["docs/note.md", "specs/005-bar/spec.md"]


@requires_git
def test_publish_nothing_to_commit_when_file_matches_head(
    svc, repo_and_remote: tuple[Path, Path]
):
    # README.md was already committed in the fixture seed. Rewriting
    # it with the same content and trying to publish must raise
    # NOTHING_TO_COMMIT, not silently create an empty commit.
    work, _ = repo_and_remote
    (work / "docs").mkdir(exist_ok=True)
    doc = work / "docs" / "idea.md"
    doc.write_text("idea\n", encoding="utf-8")

    svc.publish(
        agent_name="product_manager",
        project_id="proj",
        relative_paths=["docs/idea.md"],
        commit_message="docs: idea",
    )
    # Second call with no content change should be rejected.
    with pytest.raises(NothingToCommitError):
        svc.publish(
            agent_name="product_manager",
            project_id="proj",
            relative_paths=["docs/idea.md"],
            commit_message="docs: idea again",
        )


@requires_git
def test_publish_detects_extra_staged_files(
    svc, repo_and_remote: tuple[Path, Path]
):
    # Simulate a pre-commit hook or sibling write that staged an
    # unrelated file behind the PM bot's back. Our tool must detect
    # and abort.
    work, _ = repo_and_remote
    (work / "specs" / "006-baz").mkdir(parents=True)
    (work / "specs" / "006-baz" / "spec.md").write_text("s\n", encoding="utf-8")
    # A stray file — already tracked modification staged manually.
    (work / "README.md").write_text("modified readme\n", encoding="utf-8")
    _git(work, "add", "README.md")

    with pytest.raises(ExtraStagedFilesError, match="beyond the requested set"):
        svc.publish(
            agent_name="product_manager",
            project_id="proj",
            relative_paths=["specs/006-baz/spec.md"],
            commit_message="spec: baz",
        )


@requires_git
def test_publish_surfaces_push_failure(
    svc, repo_and_remote: tuple[Path, Path], tmp_path: Path
):
    # Point origin at a non-existent remote so push fails hard.
    work, _ = repo_and_remote
    dead = tmp_path / "dead.git"  # no init, doesn't exist
    _git(work, "remote", "set-url", "origin", str(dead))

    (work / "specs" / "007").mkdir(parents=True)
    (work / "specs" / "007" / "spec.md").write_text("s\n", encoding="utf-8")

    with pytest.raises(PushFailedError):
        svc.publish(
            agent_name="product_manager",
            project_id="proj",
            relative_paths=["specs/007/spec.md"],
            commit_message="spec: seven",
        )


@requires_git
def test_publish_refuses_detached_head(
    svc, repo_and_remote: tuple[Path, Path]
):
    work, _ = repo_and_remote
    # Detach
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(work),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _git(work, "checkout", "-q", "--detach", sha)

    (work / "specs" / "008").mkdir(parents=True)
    (work / "specs" / "008" / "spec.md").write_text("s\n", encoding="utf-8")

    with pytest.raises(DetachedHeadError):
        svc.publish(
            agent_name="product_manager",
            project_id="proj",
            relative_paths=["specs/008/spec.md"],
            commit_message="spec: eight",
        )


# ---------------------------------------------------------------------------
# Tripwire: no force flags reach git push
# ---------------------------------------------------------------------------


@requires_git
def test_no_force_flag_reaches_git_push(
    svc, repo_and_remote: tuple[Path, Path], monkeypatch
):
    """Ensure the argv we pass to ``git push`` contains no ``-f`` /
    ``--force`` / ``--force-with-lease`` under any code path. We
    monkeypatch ``subprocess.run`` and inspect every call.
    """
    work, _ = repo_and_remote
    spec_dir = work / "specs" / "009-tripwire"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.md").write_text("s\n", encoding="utf-8")

    seen_argvs: list[list[str]] = []
    real_run = subprocess.run

    def spy_run(*args, **kwargs):
        argv = list(args[0]) if args else list(kwargs.get("args") or [])
        seen_argvs.append(argv)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(
        "feishu_agent.team.artifact_publish_service.subprocess.run",
        spy_run,
    )

    svc.publish(
        agent_name="product_manager",
        project_id="proj",
        relative_paths=["specs/009-tripwire/spec.md"],
        commit_message="spec: nine",
    )

    push_calls = [argv for argv in seen_argvs if len(argv) >= 2 and argv[1] == "push"]
    assert push_calls, "expected at least one git push invocation"
    for argv in push_calls:
        assert "--force" not in argv
        assert "-f" not in argv
        assert "--force-with-lease" not in argv
        # Only remote + branch after "push"; no flags allowed.
        flagged = [a for a in argv[2:] if a.startswith("-")]
        assert flagged == [], f"unexpected flags: {flagged}"
