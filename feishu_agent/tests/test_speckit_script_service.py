"""Unit tests for ``SpeckitScriptService`` (the whitelisted runner that
lets PM execute ``.specify/scripts/bash/create-new-feature.sh`` and a
small handful of friends without granting a generic ``run_shell`` tool).

Tests fall into three buckets:

1. **Pure validation** — agent ACL, script ACL, argv regex and limits.
   These do not need git/bash and run unconditionally.
2. **End-to-end** — copy the real ``.specify/`` tree from the
   FeishuOPC repo into a temp project, init git there, run
   ``create-new-feature.sh`` via the service, and assert the branch
   was cut, the spec scaffold was written, and ``parsed_json`` carries
   ``BRANCH_NAME`` / ``SPEC_FILE`` / ``FEATURE_NUM``. Skipped when
   ``git`` or ``bash`` are not on PATH.
3. **Failure paths** — timeout (synthetic slow script), missing
   ``.specify/`` tree.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from feishu_agent.tools.speckit_script_service import (
    ScriptArgRejectedError,
    ScriptMissingError,
    ScriptNotAllowedError,
    ScriptRuntimeError,
    ScriptTimeoutError,
    SpeckitScriptService,
    UnknownProjectError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


requires_git_bash = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="git or bash binary not available",
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


# ---------------------------------------------------------------------------
# Validation (no subprocess needed)
# ---------------------------------------------------------------------------


def test_unknown_agent_rejected(tmp_path: Path):
    svc = SpeckitScriptService(project_roots={"proj": tmp_path})
    with pytest.raises(ScriptNotAllowedError) as exc:
        svc.run_script(
            agent_name="random_role",
            project_id="proj",
            script="create-new-feature.sh",
        )
    assert "no speckit scripts allowed" in exc.value.message


def test_pm_cannot_run_tl_only_script(tmp_path: Path):
    svc = SpeckitScriptService(project_roots={"proj": tmp_path})
    with pytest.raises(ScriptNotAllowedError) as exc:
        svc.run_script(
            agent_name="product_manager",
            project_id="proj",
            script="setup-plan.sh",
        )
    assert "setup-plan.sh" in exc.value.message
    assert "product_manager" in exc.value.message


def test_tl_cannot_run_pm_only_script(tmp_path: Path):
    svc = SpeckitScriptService(project_roots={"proj": tmp_path})
    with pytest.raises(ScriptNotAllowedError):
        svc.run_script(
            agent_name="tech_lead",
            project_id="proj",
            script="create-new-feature.sh",
        )


def test_unknown_project_rejected(tmp_path: Path):
    svc = SpeckitScriptService(project_roots={"proj": tmp_path})
    with pytest.raises(UnknownProjectError) as exc:
        svc.run_script(
            agent_name="product_manager",
            project_id="missing",
            script="create-new-feature.sh",
        )
    assert "missing" in exc.value.message


def test_arg_with_newline_rejected(tmp_path: Path):
    svc = SpeckitScriptService(project_roots={"proj": tmp_path})
    with pytest.raises(ScriptArgRejectedError) as exc:
        svc.run_script(
            agent_name="product_manager",
            project_id="proj",
            script="create-new-feature.sh",
            args=["line1\nline2"],
        )
    assert "newline" in exc.value.message.lower()


def test_arg_with_shell_metacharacter_rejected(tmp_path: Path):
    svc = SpeckitScriptService(project_roots={"proj": tmp_path})
    for bad_arg in ["a$b", "a`b", "a;b", "a|b", "a&b", "a>b", "a<b", "a(b", "a)b"]:
        with pytest.raises(ScriptArgRejectedError):
            svc.run_script(
                agent_name="product_manager",
                project_id="proj",
                script="create-new-feature.sh",
                args=[bad_arg],
            )


def test_arg_with_path_traversal_rejected(tmp_path: Path):
    svc = SpeckitScriptService(project_roots={"proj": tmp_path})
    with pytest.raises(ScriptArgRejectedError) as exc:
        svc.run_script(
            agent_name="product_manager",
            project_id="proj",
            script="create-new-feature.sh",
            args=["specs/../../etc/passwd"],
        )
    assert ".." in exc.value.message


def test_too_many_args_rejected(tmp_path: Path):
    svc = SpeckitScriptService(project_roots={"proj": tmp_path})
    with pytest.raises(ScriptArgRejectedError) as exc:
        svc.run_script(
            agent_name="product_manager",
            project_id="proj",
            script="create-new-feature.sh",
            args=["a"] * 17,
        )
    assert "Too many" in exc.value.message


def test_oversized_arg_rejected(tmp_path: Path):
    svc = SpeckitScriptService(project_roots={"proj": tmp_path})
    with pytest.raises(ScriptArgRejectedError) as exc:
        svc.run_script(
            agent_name="product_manager",
            project_id="proj",
            script="create-new-feature.sh",
            args=["x" * 600],
        )
    assert "500 bytes" in exc.value.message


def test_chinese_description_accepted(tmp_path: Path):
    """CJK characters should pass the regex (PM users write in Chinese)."""

    proj = tmp_path / "proj"
    proj.mkdir()
    svc = SpeckitScriptService(project_roots={"proj": proj})
    # Will fail with SCRIPT_NOT_FOUND because we didn't lay down
    # ``.specify/`` — but importantly NOT with ScriptArgRejectedError.
    with pytest.raises(ScriptMissingError):
        svc.run_script(
            agent_name="product_manager",
            project_id="proj",
            script="create-new-feature.sh",
            args=["--json", "--short-name", "vine-growth", "葡萄藤可视化模块"],
        )


def test_missing_specify_tree_rejected(tmp_path: Path):
    """Project without ``.specify/`` returns SCRIPT_NOT_FOUND, not a
    crash. PM relays this to user as 'project not initialized'."""

    proj = tmp_path / "proj"
    proj.mkdir()
    svc = SpeckitScriptService(project_roots={"proj": proj})
    with pytest.raises(ScriptMissingError) as exc:
        svc.run_script(
            agent_name="product_manager",
            project_id="proj",
            script="create-new-feature.sh",
            args=["--json", "test"],
        )
    assert "Script missing" in exc.value.message


def test_allowed_scripts_for_agent_returns_per_role_set(tmp_path: Path):
    svc = SpeckitScriptService(project_roots={"proj": tmp_path})
    pm = set(svc.allowed_scripts_for_agent("product_manager"))
    tl = set(svc.allowed_scripts_for_agent("tech_lead"))
    assert "create-new-feature.sh" in pm
    assert "create-new-feature.sh" not in tl
    assert "setup-plan.sh" in tl
    assert "setup-plan.sh" not in pm
    assert svc.allowed_scripts_for_agent("dev") == ()


# ---------------------------------------------------------------------------
# End-to-end with real .specify/ tree
# ---------------------------------------------------------------------------


@pytest.fixture
def project_with_specify(tmp_path: Path) -> Path:
    """Stand up a minimal git repo with the FeishuOPC ``.specify/`` tree
    copied in, so we can exercise create-new-feature.sh for real."""

    proj = tmp_path / "proj"
    proj.mkdir()
    src = REPO_ROOT / ".specify"
    if not src.is_dir():
        pytest.skip(".specify/ tree not present in this checkout")
    shutil.copytree(src, proj / ".specify")
    # Init a git repo with a real first commit on main, otherwise
    # `git checkout -b NNN-slug` would fail (no HEAD to branch from).
    _git(proj, "init", "-q", "-b", "main")
    (proj / "README.md").write_text("seed\n", encoding="utf-8")
    _git(proj, "add", "README.md")
    _git(proj, "commit", "-q", "-m", "seed")
    return proj


@requires_git_bash
def test_create_new_feature_end_to_end(project_with_specify: Path):
    svc = SpeckitScriptService(project_roots={"proj": project_with_specify})
    result = svc.run_script(
        agent_name="product_manager",
        project_id="proj",
        script="create-new-feature.sh",
        args=[
            "--json",
            "--short-name",
            "vine-growth",
            "Visualize vocabulary growth as a vine",
        ],
    )

    assert result.success, f"script failed: stderr={result.stderr!r}"
    assert result.exit_code == 0
    assert result.parsed_json is not None
    assert result.parsed_json["BRANCH_NAME"].endswith("-vine-growth")
    assert result.parsed_json["SPEC_FILE"].endswith("/spec.md")
    assert result.parsed_json["FEATURE_NUM"]

    branch = result.parsed_json["BRANCH_NAME"]
    spec_path = project_with_specify / result.parsed_json["SPEC_FILE"].replace(
        str(project_with_specify) + "/", ""
    )
    assert spec_path.is_file(), "spec.md scaffold not created"
    assert spec_path.stat().st_size > 0

    # Branch was actually cut and HEAD is on it.
    head = _git(project_with_specify, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert head == branch


@requires_git_bash
def test_create_new_feature_rejects_disallowed_script_even_after_setup(
    project_with_specify: Path,
):
    """With a real ``.specify/`` tree on disk, the PM agent still
    cannot reach beyond its whitelist."""

    svc = SpeckitScriptService(project_roots={"proj": project_with_specify})
    with pytest.raises(ScriptNotAllowedError):
        svc.run_script(
            agent_name="product_manager",
            project_id="proj",
            script="setup-plan.sh",
            args=["--json"],
        )


# ---------------------------------------------------------------------------
# Timeout & runtime errors
# ---------------------------------------------------------------------------


@requires_git_bash
def test_timeout_returns_typed_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Use a fake .specify/ tree with a single sleeping script and a
    1-second timeout to drive the timeout branch deterministically."""

    proj = tmp_path / "proj"
    bash_dir = proj / ".specify" / "scripts" / "bash"
    bash_dir.mkdir(parents=True)
    sleeper = bash_dir / "create-new-feature.sh"
    sleeper.write_text("#!/usr/bin/env bash\nsleep 5\n", encoding="utf-8")
    sleeper.chmod(0o755)

    svc = SpeckitScriptService(
        project_roots={"proj": proj}, timeout_seconds=1
    )
    with pytest.raises(ScriptTimeoutError):
        svc.run_script(
            agent_name="product_manager",
            project_id="proj",
            script="create-new-feature.sh",
            args=["test"],
        )


def test_missing_bash_returns_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """If bash disappears from PATH the service raises ScriptRuntimeError
    BEFORE invoking subprocess (so we can give a sensible LLM error)."""

    proj = tmp_path / "proj"
    bash_dir = proj / ".specify" / "scripts" / "bash"
    bash_dir.mkdir(parents=True)
    (bash_dir / "create-new-feature.sh").write_text("#!/bin/sh\necho ok\n")
    monkeypatch.setattr(
        "feishu_agent.tools.speckit_script_service.shutil.which",
        lambda name: None,
    )
    svc = SpeckitScriptService(project_roots={"proj": proj})
    with pytest.raises(ScriptRuntimeError) as exc:
        svc.run_script(
            agent_name="product_manager",
            project_id="proj",
            script="create-new-feature.sh",
            args=["test"],
        )
    assert "bash" in exc.value.message
