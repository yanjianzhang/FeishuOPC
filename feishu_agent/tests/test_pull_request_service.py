"""Unit tests for PullRequestService.

We do NOT hit the real GitHub API. Instead we shim ``gh`` by writing
a tiny Python script onto a temp ``PATH`` entry that prints a
well-formed PR URL on stdout. This lets us exercise all the branching
/ protection / docs-diff / env logic deterministically.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

from feishu_agent.tools.code_write_service import (
    CodeWriteAuditLog,
    CodeWritePolicy,
)
from feishu_agent.tools.pull_request_service import (
    PullRequestBranchProtectedError,
    PullRequestCommandError,
    PullRequestNotPushedError,
    PullRequestProjectError,
    PullRequestService,
    load_gh_token_from_env_file,
)

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not available"
)


# ---------------------------------------------------------------------------
# Fake `gh`
# ---------------------------------------------------------------------------


def _install_fake_gh(
    tmp_path: Path,
    *,
    rc: int = 0,
    stdout: str = "https://github.com/acme/widgets/pull/42\n",
    stderr: str = "",
    record_to: Path | None = None,
) -> Path:
    """Write a POSIX shell script named ``gh`` that prints ``stdout`` and
    exits ``rc``. Returns the directory to be prepended to PATH."""
    bin_dir = tmp_path / "fake_bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    record_line = (
        f'printf "%s\\n" "$*" >> {record_to}' if record_to is not None else ""
    )
    gh.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env sh
            {record_line}
            if [ -n {repr(stderr)} ]; then
              printf '%s' {repr(stderr)} 1>&2
            fi
            printf '%s' {repr(stdout)}
            exit {rc}
            """
        ),
        encoding="utf-8",
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


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
        allowed_write_roots=("lib/", "docs/"),
    )


@pytest.fixture
def repo_on_feature(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "lib").mkdir()
    (work / "lib" / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(work, "add", "lib/a.py")
    _git(work, "commit", "-q", "-m", "initial")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-q", "origin", "main")
    _git(work, "checkout", "-q", "-b", "feature/work")
    # Feature commit + push so upstream tracking exists:
    (work / "lib" / "b.py").write_text("y = 2\n", encoding="utf-8")
    _git(work, "add", "lib/b.py")
    _git(work, "commit", "-q", "-m", "feat")
    _git(work, "push", "-q", "-u", "origin", "feature/work")
    # Fetch so origin/main is known locally (needed for docs-diff):
    _git(work, "fetch", "-q", "origin")
    return work


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_unknown_project(tmp_path: Path, policy: CodeWritePolicy):
    svc = PullRequestService(project_roots={}, policies={})
    with pytest.raises(PullRequestProjectError):
        svc.create_pull_request(project_id="nope", title="t", body="b")


def test_validate_title_and_body(
    repo_on_feature: Path, policy: CodeWritePolicy
):
    svc = PullRequestService(
        project_roots={"proj": repo_on_feature},
        policies={"proj": policy},
    )
    with pytest.raises(Exception):
        svc.create_pull_request(project_id="proj", title="", body="ok")
    with pytest.raises(Exception):
        svc.create_pull_request(project_id="proj", title="ok", body="   ")


@requires_git
def test_refuses_on_protected_branch(
    repo_on_feature: Path, policy: CodeWritePolicy
):
    _git(repo_on_feature, "checkout", "-q", "main")
    svc = PullRequestService(
        project_roots={"proj": repo_on_feature},
        policies={"proj": policy},
    )
    with pytest.raises(PullRequestBranchProtectedError):
        svc.create_pull_request(
            project_id="proj", title="t", body="b", base="main"
        )


@requires_git
def test_refuses_when_head_equals_base(
    repo_on_feature: Path, policy: CodeWritePolicy
):
    svc = PullRequestService(
        project_roots={"proj": repo_on_feature},
        policies={"proj": policy},
    )
    # base=feature/work (same as current) → must refuse
    with pytest.raises(PullRequestBranchProtectedError):
        svc.create_pull_request(
            project_id="proj", title="t", body="b", base="feature/work"
        )


@requires_git
def test_refuses_when_branch_not_pushed(
    tmp_path: Path, policy: CodeWritePolicy
):
    """New branch that was never pushed → NotPushed."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", str(remote))
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "lib").mkdir()
    (work / "lib" / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(work, "add", "lib/a.py")
    _git(work, "commit", "-q", "-m", "initial")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-q", "origin", "main")
    _git(work, "checkout", "-q", "-b", "feature/never-pushed")
    (work / "lib" / "b.py").write_text("y = 2\n", encoding="utf-8")
    _git(work, "add", "lib/b.py")
    _git(work, "commit", "-q", "-m", "local")

    svc = PullRequestService(
        project_roots={"proj": work},
        policies={"proj": policy},
    )
    with pytest.raises(PullRequestNotPushedError):
        svc.create_pull_request(project_id="proj", title="t", body="b")


@requires_git
def test_happy_path_returns_url_and_number(
    repo_on_feature: Path, policy: CodeWritePolicy, tmp_path: Path
):
    fake_bin = _install_fake_gh(
        tmp_path, stdout="https://github.com/acme/widgets/pull/42\n"
    )
    audit = CodeWriteAuditLog(root=tmp_path / "audit", trace_id="t1")
    svc = PullRequestService(
        project_roots={"proj": repo_on_feature},
        policies={"proj": policy},
        audit_log=audit,
        gh_binary=str(fake_bin / "gh"),
    )
    res = svc.create_pull_request(
        project_id="proj",
        title="3-1: feature",
        body="Does the thing.",
    )
    assert res.url == "https://github.com/acme/widgets/pull/42"
    assert res.number == 42
    assert res.branch == "feature/work"
    assert res.base == "main"

    audit_file = tmp_path / "audit" / "t1.jsonl"
    assert '"pr_create"' in audit_file.read_text(encoding="utf-8")


@requires_git
def test_warns_when_no_docs_change(
    repo_on_feature: Path, policy: CodeWritePolicy, tmp_path: Path
):
    """PR that didn't touch docs/ must get the ⚠️ warning prepended to
    body, and warnings must be reported back."""
    fake_bin = _install_fake_gh(
        tmp_path, stdout="https://github.com/acme/widgets/pull/7\n"
    )
    svc = PullRequestService(
        project_roots={"proj": repo_on_feature},
        policies={"proj": policy},
        gh_binary=str(fake_bin / "gh"),
    )
    res = svc.create_pull_request(
        project_id="proj", title="t", body="plain body"
    )
    assert res.warnings, "expected a docs-missing warning"
    assert "No `docs/` changes" in res.body
    # Original body still present:
    assert "plain body" in res.body


@requires_git
def test_no_warning_when_docs_touched(
    repo_on_feature: Path, policy: CodeWritePolicy, tmp_path: Path
):
    (repo_on_feature / "docs").mkdir(exist_ok=True)
    (repo_on_feature / "docs" / "changelog.md").write_text(
        "# change\n", encoding="utf-8"
    )
    _git(repo_on_feature, "add", "docs/changelog.md")
    _git(repo_on_feature, "commit", "-q", "-m", "docs: changelog")
    _git(repo_on_feature, "push", "-q", "origin", "feature/work")

    fake_bin = _install_fake_gh(
        tmp_path, stdout="https://github.com/acme/widgets/pull/8\n"
    )
    svc = PullRequestService(
        project_roots={"proj": repo_on_feature},
        policies={"proj": policy},
        gh_binary=str(fake_bin / "gh"),
    )
    res = svc.create_pull_request(
        project_id="proj", title="t", body="plain body"
    )
    assert res.warnings == []
    assert res.body == "plain body"


@requires_git
def test_surfaces_gh_failure(
    repo_on_feature: Path, policy: CodeWritePolicy, tmp_path: Path
):
    """Non-zero exit from gh → PullRequestCommandError with stderr."""
    fake_bin = _install_fake_gh(
        tmp_path,
        rc=1,
        stdout="",
        stderr="gh: auth required (set GH_TOKEN or run `gh auth login`)\n",
    )
    svc = PullRequestService(
        project_roots={"proj": repo_on_feature},
        policies={"proj": policy},
        gh_binary=str(fake_bin / "gh"),
    )
    with pytest.raises(PullRequestCommandError) as exc:
        svc.create_pull_request(project_id="proj", title="t", body="b")
    assert "auth required" in str(exc.value)


@requires_git
def test_missing_gh_binary(
    repo_on_feature: Path, policy: CodeWritePolicy, tmp_path: Path
):
    svc = PullRequestService(
        project_roots={"proj": repo_on_feature},
        policies={"proj": policy},
        gh_binary=str(tmp_path / "does_not_exist"),
    )
    with pytest.raises(PullRequestCommandError) as exc:
        svc.create_pull_request(project_id="proj", title="t", body="b")
    assert "not found" in str(exc.value)


# ---------------------------------------------------------------------------
# GH_TOKEN loading & env injection
# ---------------------------------------------------------------------------


def _install_gh_env_recorder(tmp_path: Path, record_to: Path) -> Path:
    """Install a gh shim that dumps GH_TOKEN and GITHUB_TOKEN values to
    ``record_to``. Use for env-injection assertions."""
    bin_dir = tmp_path / "fake_bin_env"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env sh
            {{
              printf 'GH_TOKEN=%s\\n'     "${{GH_TOKEN-}}"
              printf 'GITHUB_TOKEN=%s\\n' "${{GITHUB_TOKEN-}}"
            }} > {record_to}
            printf 'https://github.com/acme/widgets/pull/99\\n'
            """
        ),
        encoding="utf-8",
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


class TestLoadGhTokenFromEnvFile:
    def test_bare_token(self, tmp_path: Path):
        f = tmp_path / "gh_token.env"
        f.write_text("ghp_bare123", encoding="utf-8")
        assert load_gh_token_from_env_file(f) == "ghp_bare123"

    def test_assignment(self, tmp_path: Path):
        f = tmp_path / "gh_token.env"
        f.write_text("GH_TOKEN=ghp_assign456\n", encoding="utf-8")
        assert load_gh_token_from_env_file(f) == "ghp_assign456"

    def test_export_assignment(self, tmp_path: Path):
        f = tmp_path / "gh_token.env"
        f.write_text('export GH_TOKEN="ghp_export789"\n', encoding="utf-8")
        assert load_gh_token_from_env_file(f) == "ghp_export789"

    def test_github_token_alias(self, tmp_path: Path):
        f = tmp_path / "gh_token.env"
        f.write_text("GITHUB_TOKEN=ghp_alias\n", encoding="utf-8")
        assert load_gh_token_from_env_file(f) == "ghp_alias"

    def test_ignores_unrelated_vars(self, tmp_path: Path):
        f = tmp_path / "gh_token.env"
        f.write_text("FOO=bar\nBAZ=qux\n", encoding="utf-8")
        assert load_gh_token_from_env_file(f) is None

    def test_missing_file_returns_none(self, tmp_path: Path):
        assert load_gh_token_from_env_file(tmp_path / "nope.env") is None

    def test_skips_comments_and_blanks(self, tmp_path: Path):
        f = tmp_path / "gh_token.env"
        f.write_text(
            "# header comment\n\n   \nGH_TOKEN=ghp_good\n", encoding="utf-8"
        )
        assert load_gh_token_from_env_file(f) == "ghp_good"


@requires_git
def test_gh_env_injects_token_from_file(
    repo_on_feature: Path, policy: CodeWritePolicy, tmp_path: Path
):
    """When gh_token_path is provided, GH_TOKEN must reach ``gh``'s
    subprocess env — regardless of whether the parent process has it."""
    record = tmp_path / "env.dump"
    fake_bin = _install_gh_env_recorder(tmp_path, record)

    token_file = tmp_path / "gh_token.env"
    token_file.write_text("export GH_TOKEN=ghp_FROM_FILE\n", encoding="utf-8")

    # Ensure ambient env doesn't already carry one — isolate the test
    orig_gh = os.environ.pop("GH_TOKEN", None)
    orig_github = os.environ.pop("GITHUB_TOKEN", None)
    try:
        svc = PullRequestService(
            project_roots={"proj": repo_on_feature},
            policies={"proj": policy},
            gh_binary=str(fake_bin / "gh"),
            gh_token_path=token_file,
        )
        svc.create_pull_request(project_id="proj", title="t", body="b")
    finally:
        if orig_gh is not None:
            os.environ["GH_TOKEN"] = orig_gh
        if orig_github is not None:
            os.environ["GITHUB_TOKEN"] = orig_github

    dumped = record.read_text(encoding="utf-8")
    assert "GH_TOKEN=ghp_FROM_FILE" in dumped
    # GITHUB_TOKEN is populated as a cross-compat alias:
    assert "GITHUB_TOKEN=ghp_FROM_FILE" in dumped


@requires_git
def test_gh_env_falls_back_to_ambient_token(
    repo_on_feature: Path, policy: CodeWritePolicy, tmp_path: Path
):
    """If no token file is configured but the ambient env has
    GH_TOKEN, it must pass through (so ``gh auth login``-less CI still
    works)."""
    record = tmp_path / "env.dump"
    fake_bin = _install_gh_env_recorder(tmp_path, record)

    orig = os.environ.get("GH_TOKEN")
    os.environ["GH_TOKEN"] = "ghp_AMBIENT"
    try:
        svc = PullRequestService(
            project_roots={"proj": repo_on_feature},
            policies={"proj": policy},
            gh_binary=str(fake_bin / "gh"),
            # gh_token_path intentionally omitted
        )
        svc.create_pull_request(project_id="proj", title="t", body="b")
    finally:
        if orig is None:
            os.environ.pop("GH_TOKEN", None)
        else:
            os.environ["GH_TOKEN"] = orig

    dumped = record.read_text(encoding="utf-8")
    assert "GH_TOKEN=ghp_AMBIENT" in dumped


@requires_git
def test_file_token_overrides_ambient(
    repo_on_feature: Path, policy: CodeWritePolicy, tmp_path: Path
):
    """File-based token wins over ambient env — operators rotate by
    editing one file, not N shells."""
    record = tmp_path / "env.dump"
    fake_bin = _install_gh_env_recorder(tmp_path, record)

    token_file = tmp_path / "gh_token.env"
    token_file.write_text("GH_TOKEN=ghp_FILE_WINS\n", encoding="utf-8")

    orig = os.environ.get("GH_TOKEN")
    os.environ["GH_TOKEN"] = "ghp_AMBIENT_LOSES"
    try:
        svc = PullRequestService(
            project_roots={"proj": repo_on_feature},
            policies={"proj": policy},
            gh_binary=str(fake_bin / "gh"),
            gh_token_path=token_file,
        )
        svc.create_pull_request(project_id="proj", title="t", body="b")
    finally:
        if orig is None:
            os.environ.pop("GH_TOKEN", None)
        else:
            os.environ["GH_TOKEN"] = orig

    dumped = record.read_text(encoding="utf-8")
    assert "GH_TOKEN=ghp_FILE_WINS" in dumped
    assert "ghp_AMBIENT_LOSES" not in dumped
