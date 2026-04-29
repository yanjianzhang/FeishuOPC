"""Tests for ``BugFixerExecutor``.

Scope & intent
--------------
``BugFixerExecutor`` is a subclass of ``DeveloperExecutor`` with a
different ``role_name`` default. Functionally the two share the same
tool surface and same trust filter; the trust split (developer
greenfields, bug_fixer remediates) lives in the skill docs, not in
the code.

We still pin the important invariants here:

1. Subclassing really does produce the same exact surface as
   ``DeveloperExecutor`` (no mixin bug introduces drift).
2. Default ``role_name`` is ``"bug_fixer"`` — this is what the role
   registry and the runtime dispatch key on.
3. ``isinstance(BugFixerExecutor, DeveloperExecutor)`` holds so any
   future invariant we teach the developer class transfers for free.
4. The skill doc frontmatter (``skills/roles/bug_fixer.md``) stays
   in sync with the executor's actual advertised tools.

If we ever want to *diverge* (e.g. tighter size cap, require
``review_artifact`` in reason) this file is the canonical place to
assert the divergence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from feishu_agent.roles.role_executors.bug_fixer import BugFixerExecutor
from feishu_agent.roles.role_executors.developer import (
    DEVELOPER_CODE_WRITE_ALLOW,
    DeveloperExecutor,
)
from feishu_agent.roles.role_registry_service import RoleRegistryService
from feishu_agent.team.role_artifact_writer import RoleArtifactWriter
from feishu_agent.team.sprint_state_service import SprintStateService
from feishu_agent.tools.code_write_service import CodeWriteService
from feishu_agent.tools.git_ops_service import GitOpsService

ROLES_DIR = Path(__file__).resolve().parents[3] / "skills" / "roles"


def _make_sprint(tmp_path: Path, data: dict[str, Any] | None = None) -> SprintStateService:
    status_file = "sprint-status.yaml"
    (tmp_path / status_file).write_text(
        yaml.safe_dump(
            data
            or {
                "sprint_name": "Sprint 3",
                "current_sprint": {"goal": "Fix review for 3-1"},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return SprintStateService(tmp_path, status_file)


def _make_executor_full(tmp_path: Path) -> BugFixerExecutor:
    from feishu_agent.tools.workflow_service import WorkflowService

    return BugFixerExecutor(
        sprint_state_service=_make_sprint(tmp_path),
        code_write_service=MagicMock(spec=CodeWriteService),
        git_ops_service=MagicMock(spec=GitOpsService),
        role_artifact_writer=RoleArtifactWriter(
            role_name="bug_fixer",
            project_id="proj-a",
            allowed_write_root=tmp_path / "artifacts",
        ),
        # Read-only workflow surface (bmad:correct-course). See
        # reviewer / developer tests for the same pattern.
        workflow_service=MagicMock(spec=WorkflowService),
        project_id="proj-a",
    )


# ---------------------------------------------------------------------------
# Subclass invariant
# ---------------------------------------------------------------------------


def test_bug_fixer_is_developer_subclass():
    """Protects the shared-wiring assumption in feishu_runtime_service:
    same constructor signature, same behavior."""
    assert issubclass(BugFixerExecutor, DeveloperExecutor)


def test_bug_fixer_default_role_name(tmp_path: Path):
    executor = BugFixerExecutor(sprint_state_service=_make_sprint(tmp_path))
    assert executor.role_name == "bug_fixer"


def test_bug_fixer_accepts_role_name_override(tmp_path: Path):
    """We don't currently use this, but keep the override open — it
    matches developer and keeps the runtime code uniform."""
    executor = BugFixerExecutor(
        sprint_state_service=_make_sprint(tmp_path),
        role_name="bug_fixer",
    )
    assert executor.role_name == "bug_fixer"


# ---------------------------------------------------------------------------
# Surface parity with developer
# ---------------------------------------------------------------------------


EXPECTED_FULL_SURFACE = {
    "read_sprint_status",
    "describe_code_write_policy",
    "read_project_code",
    "list_project_paths",
    "write_project_code",
    "write_project_code_batch",
    # Intentionally NO git_sync_remote / start_work_branch /
    # git_push / git_fetch: branch lifecycle is tech-lead-only.
    # The TL cuts a fresh branch from origin/main before dispatching
    # developer / bug_fixer, so the sub-role has nothing to sync.
    "git_commit",
    "write_role_artifact",
    # Read-only workflow tools (bmad:correct-course). Write tools
    # (write_workflow_artifact / write_workflow_artifacts) are stripped
    # by ``_workflow_readonly = True`` in the executor.
    "read_workflow_instruction",
    "list_workflow_artifacts",
    "read_repo_file",
}


def test_full_surface_matches_developer(tmp_path: Path):
    executor = _make_executor_full(tmp_path)
    names = {s.name for s in executor.tool_specs()}
    assert names == EXPECTED_FULL_SURFACE


def test_same_allow_set_as_developer():
    """bug_fixer inherits ``_code_write_tool_allow`` =
    DEVELOPER_CODE_WRITE_ALLOW from the parent. Pin that so a future
    subclass override doesn't silently widen the surface."""
    fixer = BugFixerExecutor(sprint_state_service=MagicMock(spec=SprintStateService))
    assert fixer._code_write_tool_allow == DEVELOPER_CODE_WRITE_ALLOW


def test_git_push_never_in_surface(tmp_path: Path):
    from feishu_agent.tools.ci_watch_service import CIWatchService

    executor = _make_executor_full(tmp_path)
    # Sneak a CIWatchService onto the executor to simulate a wiring
    # regression — the filter must still hide watch_pr_checks.
    executor._ci_watch = MagicMock(spec=CIWatchService)

    names = {s.name for s in executor.tool_specs()}
    assert "git_push" not in names
    assert "run_pre_push_inspection" not in names
    assert "create_pull_request" not in names
    assert "watch_pr_checks" not in names


def test_branch_lifecycle_tools_never_in_surface(tmp_path: Path):
    """Regression: developer / bug_fixer MUST NOT have access to any
    branch-lifecycle tool. The tech lead has already cut a fresh
    branch from origin/main via ``start_work_branch`` before
    dispatching, so there is nothing for the sub-role to sync /
    fetch / switch. Before this boundary was enforced, the developer
    would helpfully call ``git_sync_remote`` on a just-created
    branch, which at best wasted a turn and at worst re-armed dirty
    state the TL had just normalized."""
    executor = _make_executor_full(tmp_path)
    names = {s.name for s in executor.tool_specs()}
    for forbidden in {
        "start_work_branch",
        "git_sync_remote",
        "git_fetch",
        "git_pull",
        "git_checkout",
    }:
        assert forbidden not in names, (
            f"{forbidden} leaked into bug_fixer / developer surface; "
            "branch lifecycle is tech-lead-only."
        )


# ---------------------------------------------------------------------------
# Dispatch-time refusal (belt + suspenders — same as developer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_git_push_refused_at_dispatch(tmp_path: Path):
    git_ops = MagicMock(spec=GitOpsService)
    executor = BugFixerExecutor(
        sprint_state_service=_make_sprint(tmp_path),
        code_write_service=MagicMock(spec=CodeWriteService),
        git_ops_service=git_ops,
    )
    result = await executor.execute_tool(
        "git_push", {"inspection_token": "x"}
    )
    assert isinstance(result, dict)
    assert result["error"] == "TOOL_NOT_ALLOWED_ON_ROLE"
    git_ops.push_current_branch.assert_not_called()


@pytest.mark.asyncio
async def test_create_pull_request_refused_at_dispatch(tmp_path: Path):
    executor = _make_executor_full(tmp_path)
    result = await executor.execute_tool(
        "create_pull_request", {"title": "t", "body": "b"}
    )
    assert isinstance(result, dict)
    assert result["error"] == "TOOL_NOT_ALLOWED_ON_ROLE"


@pytest.mark.asyncio
async def test_watch_pr_checks_refused_at_dispatch(tmp_path: Path):
    """bug_fixer must never have the post-PR CI gate. The gate is how
    TL decides the delivery is done; a sub-role deciding it would
    invert the trust boundary."""
    from feishu_agent.tools.ci_watch_service import CIWatchService

    executor = _make_executor_full(tmp_path)
    executor._ci_watch = MagicMock(spec=CIWatchService)

    result = await executor.execute_tool(
        "watch_pr_checks", {"pr_number": 1}
    )
    assert isinstance(result, dict)
    assert result["error"] == "TOOL_NOT_ALLOWED_ON_ROLE"
    executor._ci_watch.watch.assert_not_called()


# ---------------------------------------------------------------------------
# Happy path — delegates to the same services developer uses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_project_code_delegates(tmp_path: Path):
    code_write = MagicMock(spec=CodeWriteService)
    code_write.write_source.return_value = {
        "path": "src/fix.py",
        "bytes_written": 12,
        "is_new_file": False,
    }
    executor = BugFixerExecutor(
        sprint_state_service=_make_sprint(tmp_path),
        code_write_service=code_write,
        project_id="proj-a",
    )
    result = await executor.execute_tool(
        "write_project_code",
        {
            "relative_path": "src/fix.py",
            "content": "patched = True",
            "reason": "3-1: fix review blocker #2 — null-guard",
        },
    )
    assert result["path"] == "src/fix.py"
    code_write.write_source.assert_called_once()


@pytest.mark.asyncio
async def test_write_role_artifact_lands_in_fixes_dir(tmp_path: Path):
    """The runtime wires bug_fixer's artifact root to
    ``docs/implementation/fixes/``; here we just check the executor
    persists through RoleArtifactWriter without special-casing."""
    executor = _make_executor_full(tmp_path)
    result = await executor.execute_tool(
        "write_role_artifact",
        {
            "path": "3-1-fix.md",
            "content": "# Fix\nBlocker #1: addressed in abc1234\n",
            "summary": "Fixed 1 blocker for 3-1",
        },
    )
    assert isinstance(result, dict)
    assert result.get("error") is None
    assert (tmp_path / "artifacts" / "3-1-fix.md").exists()


# ---------------------------------------------------------------------------
# Frontmatter parity
# ---------------------------------------------------------------------------


def test_tool_allow_list_matches_role_file(tmp_path: Path):
    registry = RoleRegistryService(ROLES_DIR)
    role = registry.get_role("bug_fixer")
    executor = _make_executor_full(tmp_path)
    assert set(role.tool_allow_list) == {s.name for s in executor.tool_specs()}
