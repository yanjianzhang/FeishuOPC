"""Unit tests for PrePushInspector.

We drive a real git repo inside ``tmp_path``. The inspector shells out
to ``git``; these tests require ``git`` on PATH (same precondition as
the rest of the agent runtime).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from feishu_agent.tools.code_write_service import (
    CodeWritePolicy,
    ValidationCommand,
)
from feishu_agent.tools.pre_push_inspector import (
    InspectionGitError,
    InspectionProjectError,
    PrePushInspector,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not available"
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "HOME": str(cwd),
            "PATH": __import__("os").environ.get("PATH", ""),
        },
    )


@pytest.fixture
def policy() -> CodeWritePolicy:
    return CodeWritePolicy(
        allowed_write_roots=("lib/", "test/"),
        require_confirmation_above_bytes=200,
        max_files_per_write_batch=10,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A fresh git repo with an initial commit on a feature branch."""
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "lib").mkdir()
    (root / "lib" / "initial.py").write_text("print('hi')\n", encoding="utf-8")
    _git(root, "add", "lib/initial.py")
    _git(root, "commit", "-q", "-m", "initial")
    _git(root, "checkout", "-q", "-b", "feature/work")
    return root


@pytest.fixture
def inspector(repo: Path, policy: CodeWritePolicy) -> PrePushInspector:
    return PrePushInspector(
        project_roots={"proj": repo},
        policies={"proj": policy},
    )


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_inspect_unknown_project(inspector: PrePushInspector):
    with pytest.raises(InspectionProjectError):
        inspector.inspect("nope")


def test_inspect_non_git_dir(tmp_path: Path, policy: CodeWritePolicy):
    not_a_repo = tmp_path / "nogit"
    not_a_repo.mkdir()
    ins = PrePushInspector(
        project_roots={"proj": not_a_repo},
        policies={"proj": policy},
    )
    with pytest.raises(InspectionGitError):
        ins.inspect("proj")


# ---------------------------------------------------------------------------
# Clean report → token issued
# ---------------------------------------------------------------------------


@requires_git
def test_clean_repo_no_changes_issues_token(inspector: PrePushInspector):
    report = inspector.inspect("proj")
    assert report.ok
    assert report.blockers == []
    assert report.branch == "feature/work"
    assert report.inspection_token, "ok=true should mint a token"
    assert report.is_protected_branch is False


@requires_git
def test_small_clean_change_issues_token(
    inspector: PrePushInspector, repo: Path
):
    (repo / "lib" / "feature.py").write_text("print('feat')\n", encoding="utf-8")
    _git(repo, "add", "lib/feature.py")
    report = inspector.inspect("proj")
    assert report.ok, f"blockers: {report.blockers}"
    assert report.inspection_token
    assert any(f.path == "lib/feature.py" for f in report.files_changed)


# ---------------------------------------------------------------------------
# Blockers: secrets, path violations, denied segments
# ---------------------------------------------------------------------------


@requires_git
def test_secret_in_diff_is_blocker(inspector: PrePushInspector, repo: Path):
    (repo / "lib" / "bad.py").write_text(
        "TOKEN = 'ghp_" + "a" * 40 + "'\n", encoding="utf-8"
    )
    _git(repo, "add", "lib/bad.py")
    report = inspector.inspect("proj")
    assert not report.ok
    assert any(i.kind == "secret_in_diff" for i in report.blockers)
    assert report.inspection_token is None, "token must NOT be issued on blockers"


@requires_git
def test_secret_in_untracked_is_blocker(
    inspector: PrePushInspector, repo: Path
):
    (repo / "lib" / "untracked.py").write_text(
        "AKIAIOSFODNN7EXAMPLE\n", encoding="utf-8"
    )
    report = inspector.inspect("proj")
    assert not report.ok
    assert any(i.kind == "secret_in_untracked" for i in report.blockers)
    assert "lib/untracked.py" in report.untracked_files


@requires_git
def test_path_outside_policy_is_blocker(
    inspector: PrePushInspector, repo: Path
):
    (repo / "server").mkdir()
    (repo / "server" / "main.py").write_text(
        "print('server')\n", encoding="utf-8"
    )
    _git(repo, "add", "server/main.py")
    report = inspector.inspect("proj")
    assert not report.ok
    assert any(
        i.kind == "path_outside_policy" and i.path == "server/main.py"
        for i in report.blockers
    )


@requires_git
def test_denied_path_segment_is_blocker(
    inspector: PrePushInspector, repo: Path
):
    # .env inside an allowed root still gets flagged via denied segment.
    (repo / "lib" / ".env").write_text("SECRET=1\n", encoding="utf-8")
    _git(repo, "add", "lib/.env")
    report = inspector.inspect("proj")
    assert not report.ok
    blockers = [b for b in report.blockers if b.path == "lib/.env"]
    assert blockers, "lib/.env should have been blocked"


# ---------------------------------------------------------------------------
# Warnings: oversize
# ---------------------------------------------------------------------------


@requires_git
def test_oversize_is_warning_not_blocker(
    inspector: PrePushInspector, repo: Path
):
    # Generate many clean lines to cross the approx-bytes threshold.
    big = "\n".join(f"x = {i}" for i in range(200)) + "\n"
    (repo / "lib" / "big.py").write_text(big, encoding="utf-8")
    _git(repo, "add", "lib/big.py")
    report = inspector.inspect("proj")
    assert any(
        w.kind == "oversize_change" and w.path == "lib/big.py"
        for w in report.warnings
    )
    # Oversize alone is a warning; report should still be ok.
    assert report.ok


# ---------------------------------------------------------------------------
# Protected branch flag
# ---------------------------------------------------------------------------


@requires_git
def test_protected_branch_flag_set_on_main(
    inspector: PrePushInspector, repo: Path
):
    _git(repo, "checkout", "-q", "main")
    report = inspector.inspect("proj")
    assert report.branch == "main"
    assert report.is_protected_branch is True


# ---------------------------------------------------------------------------
# Token semantics
# ---------------------------------------------------------------------------


@requires_git
def test_token_consumed_once(inspector: PrePushInspector, repo: Path):
    report = inspector.inspect("proj")
    assert report.inspection_token
    ok1 = inspector.consume_token(
        report.inspection_token,
        expected_head_sha=report.head_sha,
        expected_branch=report.branch,
    )
    assert ok1 is True
    # Second consume must fail (replay protection).
    ok2 = inspector.consume_token(
        report.inspection_token,
        expected_head_sha=report.head_sha,
        expected_branch=report.branch,
    )
    assert ok2 is False


@requires_git
def test_token_refuses_if_head_changed(
    inspector: PrePushInspector, repo: Path
):
    report = inspector.inspect("proj")
    assert report.inspection_token
    # Make a new commit: HEAD moves.
    (repo / "lib" / "x.py").write_text("print('x')\n", encoding="utf-8")
    _git(repo, "add", "lib/x.py")
    _git(repo, "commit", "-q", "-m", "mutate")
    new_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert new_head != report.head_sha
    ok = inspector.consume_token(
        report.inspection_token,
        expected_head_sha=new_head,
        expected_branch=report.branch,
    )
    assert ok is False, "token bound to old HEAD must not match new HEAD"


@requires_git
def test_token_ttl_expires(repo: Path, policy: CodeWritePolicy):
    clock = {"t": 1000.0}

    def fake_now() -> float:
        return clock["t"]

    ins = PrePushInspector(
        project_roots={"proj": repo},
        policies={"proj": policy},
        now=fake_now,
    )
    report = ins.inspect("proj")
    assert report.inspection_token
    clock["t"] += ins.TOKEN_TTL_SECONDS + 1
    ok = ins.consume_token(
        report.inspection_token,
        expected_head_sha=report.head_sha,
        expected_branch=report.branch,
    )
    assert ok is False


# ---------------------------------------------------------------------------
# Validation commands (project-configured pre-push gates)
# ---------------------------------------------------------------------------
#
# These tests pin the contract added so the tech-lead bot stops minting
# inspection_tokens for code that wouldn't pass GitHub Actions. The legacy
# behavior (no validation_commands → secret/path/size only) MUST stay
# intact for projects that haven't opted in.


def _make_inspector_with_validation(
    repo: Path,
    *,
    cmd: list[str],
    name: str = "fast-check",
    cwd: str = "",
    timeout_seconds: int = 30,
) -> PrePushInspector:
    pol = CodeWritePolicy(
        allowed_write_roots=("lib/", "test/"),
        require_confirmation_above_bytes=1_000_000,  # disable oversize noise
        max_files_per_write_batch=10,
        validation_commands=(
            ValidationCommand(
                name=name,
                cmd=tuple(cmd),
                cwd=cwd,
                timeout_seconds=timeout_seconds,
            ),
        ),
    )
    return PrePushInspector(project_roots={"proj": repo}, policies={"proj": pol})


@requires_git
def test_validation_command_success_does_not_block(repo: Path):
    ins = _make_inspector_with_validation(repo, cmd=["true"])
    report = ins.inspect("proj")
    assert report.ok, f"blockers: {report.blockers}"
    assert report.inspection_token


@requires_git
def test_validation_command_failure_becomes_blocker(repo: Path):
    ins = _make_inspector_with_validation(
        repo,
        name="miniapp-typecheck",
        cmd=["sh", "-c", "echo 'TS2322 type mismatch' >&2; exit 1"],
    )
    report = ins.inspect("proj")
    assert not report.ok
    val_blockers = [b for b in report.blockers if b.kind == "validation_failed"]
    assert val_blockers, f"expected validation_failed blocker, got {report.blockers}"
    assert val_blockers[0].path == "miniapp-typecheck"
    assert val_blockers[0].severity == "blocker"
    assert "TS2322" in val_blockers[0].detail
    assert report.inspection_token is None


@requires_git
def test_validation_command_timeout_becomes_blocker(repo: Path):
    ins = _make_inspector_with_validation(
        repo,
        name="hung-check",
        cmd=["sh", "-c", "sleep 5"],
        timeout_seconds=1,
    )
    report = ins.inspect("proj")
    assert not report.ok
    val_blockers = [b for b in report.blockers if b.kind == "validation_failed"]
    assert val_blockers and val_blockers[0].path == "hung-check"
    assert "timed out" in val_blockers[0].detail
    assert report.inspection_token is None


@requires_git
def test_validation_command_missing_binary_becomes_blocker(repo: Path):
    ins = _make_inspector_with_validation(
        repo,
        name="needs-tool",
        cmd=["definitely-not-a-real-binary-xyzzy", "--check"],
    )
    report = ins.inspect("proj")
    assert not report.ok
    val_blockers = [b for b in report.blockers if b.kind == "validation_failed"]
    assert val_blockers
    assert "binary not found" in val_blockers[0].detail
    assert report.inspection_token is None


@requires_git
def test_empty_validation_commands_preserves_legacy_behavior(
    inspector: PrePushInspector, repo: Path
):
    # `inspector` fixture uses a CodeWritePolicy WITHOUT validation_commands;
    # this asserts that absent / empty config equals "no validation step ran"
    # (i.e. backward-compat for every project that hasn't opted in).
    (repo / "lib" / "feature.py").write_text("print('feat')\n", encoding="utf-8")
    _git(repo, "add", "lib/feature.py")
    report = inspector.inspect("proj")
    assert report.ok
    assert all(
        b.kind != "validation_failed" for b in report.blockers + report.warnings
    )
