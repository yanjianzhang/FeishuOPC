"""Tests for ``DeveloperExecutor``.

Scope & intent
--------------
The developer is the only non-TL role executor that can write source
code. That makes the trust boundary VERY concrete — we pin it here:

- With only ``sprint_state_service`` wired, the surface is just
  ``read_sprint_status`` (no leakage of code-write if the runtime
  forgets to inject services).
- With code_write + git_ops + role_artifact_writer wired, the surface
  is *exactly* the whitelist — not more. In particular:
  ``git_push`` / ``create_pull_request`` / ``run_pre_push_inspection``
  stay out even though ``git_ops_service`` is present, because the
  ``_code_write_tool_allow`` filter strips them.
- Even if a stale LLM context somehow names a filtered tool, the
  dispatcher returns ``TOOL_NOT_ALLOWED_ON_ROLE`` rather than
  executing it (belt + suspenders).
- Frontmatter on ``skills/roles/developer.md`` matches what the
  executor actually advertises — role YAML and role code can't drift.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from feishu_agent.roles.role_executors.developer import (
    DEVELOPER_CODE_WRITE_ALLOW,
    DEVELOPER_LOCAL_TOOL_SPECS,
    DeveloperExecutor,
)
from feishu_agent.roles.role_registry_service import RoleRegistryService
from feishu_agent.team.role_artifact_writer import RoleArtifactWriter
from feishu_agent.team.sprint_state_service import SprintStateService
from feishu_agent.tools.code_write_service import CodeWriteService
from feishu_agent.tools.git_ops_service import GitOpsService
from feishu_agent.tools.pre_push_inspector import PrePushInspector
from feishu_agent.tools.pull_request_service import PullRequestService

ROLES_DIR = Path(__file__).resolve().parents[3] / "skills" / "roles"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sprint(tmp_path: Path, data: dict[str, Any] | None = None) -> SprintStateService:
    status_file = "sprint-status.yaml"
    (tmp_path / status_file).write_text(
        yaml.safe_dump(
            data or {"sprint_name": "Sprint 3", "current_sprint": {"goal": "Ship 3-1"}},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return SprintStateService(tmp_path, status_file)


def _make_writer(tmp_path: Path) -> RoleArtifactWriter:
    return RoleArtifactWriter(
        role_name="developer",
        project_id="proj-a",
        allowed_write_root=tmp_path,
    )


def _make_executor_minimal(tmp_path: Path) -> DeveloperExecutor:
    return DeveloperExecutor(
        sprint_state_service=_make_sprint(tmp_path),
        project_id="proj-a",
    )


def _make_executor_full(
    tmp_path: Path,
    *,
    code_write: CodeWriteService | None = None,
    git_ops: GitOpsService | None = None,
) -> DeveloperExecutor:
    from feishu_agent.tools.workflow_service import WorkflowService

    return DeveloperExecutor(
        sprint_state_service=_make_sprint(tmp_path),
        code_write_service=code_write or MagicMock(spec=CodeWriteService),
        git_ops_service=git_ops or MagicMock(spec=GitOpsService),
        role_artifact_writer=_make_writer(tmp_path / "artifacts"),
        # Read-only workflow surface. A MagicMock is enough to make
        # ``self._workflow is not None`` truthy so tool_specs() exposes
        # read_workflow_instruction / list_workflow_artifacts /
        # read_repo_file — matching the role file's tool_allow_list.
        workflow_service=MagicMock(spec=WorkflowService),
        project_id="proj-a",
    )


# ---------------------------------------------------------------------------
# Surface: minimal wiring
# ---------------------------------------------------------------------------


def test_minimal_surface_is_just_sprint_read(tmp_path: Path):
    executor = _make_executor_minimal(tmp_path)
    names = {s.name for s in executor.tool_specs()}
    assert names == {"read_sprint_status"}


def test_local_tool_specs_pinned():
    """If anyone adds a second local tool, they must update the test
    so we re-review the trust implications."""
    names = {s.name for s in DEVELOPER_LOCAL_TOOL_SPECS}
    assert names == {"read_sprint_status"}


# ---------------------------------------------------------------------------
# Surface: full wiring — exact whitelist
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
    # the developer, so the sub-role has nothing to sync.
    "git_commit",
    "write_role_artifact",
    # Read-only workflow tools — let developer load bmad:dev-story
    # methodology. The write tools (write_workflow_artifact /
    # write_workflow_artifacts) are stripped by the readonly flag.
    "read_workflow_instruction",
    "list_workflow_artifacts",
    "read_repo_file",
}


def test_full_surface_is_exactly_the_whitelist(tmp_path: Path):
    executor = _make_executor_full(tmp_path)
    names = {s.name for s in executor.tool_specs()}
    assert names == EXPECTED_FULL_SURFACE


def test_git_push_not_in_surface_even_with_git_ops(tmp_path: Path):
    """Critical: even when ``git_ops_service`` is wired in (we need it
    for ``git_commit``), ``git_push`` must stay out."""
    executor = _make_executor_full(tmp_path)
    names = {s.name for s in executor.tool_specs()}
    assert "git_push" not in names


def test_branch_lifecycle_tools_never_in_surface(tmp_path: Path):
    """Regression: the developer MUST NOT have access to any
    branch-lifecycle tool. The tech lead owns ``start_work_branch``
    (which already does ``fetch + reset --hard origin/main + checkout -b``)
    before dispatching us, so there's nothing for the developer to
    sync / fetch / switch. Before this boundary was enforced, the
    developer would helpfully call ``git_sync_remote`` on a just-
    created branch, burning a whole LLM turn on a no-op and risking
    a fast-forward that invalidates the TL's just-cut base."""
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
            f"{forbidden} leaked into developer surface; "
            "branch lifecycle is tech-lead-only."
        )


def test_pre_push_and_pr_never_in_surface(tmp_path: Path):
    """Pre-push inspection, PR creation, and post-PR CI watch are
    gatekeeper tools. Developer must not see them even if a future
    wiring bug somehow passes their services in."""
    from feishu_agent.tools.ci_watch_service import CIWatchService

    full = DeveloperExecutor(
        sprint_state_service=_make_sprint(tmp_path),
        code_write_service=MagicMock(spec=CodeWriteService),
        git_ops_service=MagicMock(spec=GitOpsService),
        role_artifact_writer=_make_writer(tmp_path / "art"),
    )
    # Sneak the services past the constructor — this is what a bad
    # refactor could look like.
    full._pre_push_inspector = MagicMock(spec=PrePushInspector)
    full._pull_request = MagicMock(spec=PullRequestService)
    full._ci_watch = MagicMock(spec=CIWatchService)

    names = {s.name for s in full.tool_specs()}
    assert "run_pre_push_inspection" not in names
    assert "create_pull_request" not in names
    assert "watch_pr_checks" not in names


def test_developer_allow_set_pinned():
    """Hardcode the exact allow set — additions must come through code
    review. Note: no branch-lifecycle tools allowed (see
    ``test_branch_lifecycle_tools_never_in_surface`` for why)."""
    assert DEVELOPER_CODE_WRITE_ALLOW == frozenset(
        {
            "describe_code_write_policy",
            "read_project_code",
            "list_project_paths",
            "write_project_code",
            "write_project_code_batch",
            "git_commit",
        }
    )


# ---------------------------------------------------------------------------
# Dispatch-time refusal (belt + suspenders)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calling_git_push_is_refused_at_dispatch(tmp_path: Path):
    """Even if the LLM somehow names ``git_push`` (stale context,
    hallucination), the mixin must refuse BEFORE touching git_ops."""
    git_ops = MagicMock(spec=GitOpsService)
    executor = _make_executor_full(tmp_path, git_ops=git_ops)

    result = await executor.execute_tool(
        "git_push",
        {"inspection_token": "deadbeef", "remote": "origin"},
    )
    assert isinstance(result, dict)
    assert result["error"] == "TOOL_NOT_ALLOWED_ON_ROLE"
    git_ops.push_current_branch.assert_not_called()


@pytest.mark.asyncio
async def test_calling_create_pull_request_is_refused_at_dispatch(tmp_path: Path):
    executor = _make_executor_full(tmp_path)
    result = await executor.execute_tool(
        "create_pull_request",
        {"title": "t", "body": "b"},
    )
    assert isinstance(result, dict)
    assert result["error"] == "TOOL_NOT_ALLOWED_ON_ROLE"


@pytest.mark.asyncio
async def test_calling_run_pre_push_inspection_is_refused_at_dispatch(tmp_path: Path):
    executor = _make_executor_full(tmp_path)
    # force-set the inspector so we can prove the filter runs before it
    executor._pre_push_inspector = MagicMock(spec=PrePushInspector)
    result = await executor.execute_tool("run_pre_push_inspection", {})
    assert isinstance(result, dict)
    assert result["error"] == "TOOL_NOT_ALLOWED_ON_ROLE"
    executor._pre_push_inspector.inspect.assert_not_called()


@pytest.mark.asyncio
async def test_calling_watch_pr_checks_is_refused_at_dispatch(tmp_path: Path):
    """Post-PR CI gate is TL-only. If a stale LLM context names it on
    the developer, the dispatcher must refuse BEFORE touching the
    service — otherwise the developer could declare PR merge-readiness,
    bypassing the tech-lead's gate."""
    from feishu_agent.tools.ci_watch_service import CIWatchService

    executor = _make_executor_full(tmp_path)
    # Sneak the service onto the executor — same shape as a bad wiring
    # refactor would produce.
    executor._ci_watch = MagicMock(spec=CIWatchService)

    result = await executor.execute_tool(
        "watch_pr_checks", {"pr_number": 1}
    )
    assert isinstance(result, dict)
    assert result["error"] == "TOOL_NOT_ALLOWED_ON_ROLE"
    executor._ci_watch.watch.assert_not_called()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_sprint_status_happy_path(tmp_path: Path):
    executor = DeveloperExecutor(
        sprint_state_service=_make_sprint(
            tmp_path,
            {
                "sprint_name": "Sprint 7",
                "current_sprint": {"goal": "Deliver developer role"},
            },
        ),
        project_id="proj-a",
    )
    result = await executor.execute_tool("read_sprint_status", {})
    assert result["sprint_name"] == "Sprint 7"
    assert result["goal"] == "Deliver developer role"


@pytest.mark.asyncio
async def test_write_project_code_delegates_to_service(tmp_path: Path):
    code_write = MagicMock(spec=CodeWriteService)
    code_write.write_source.return_value = {
        "path": "example_app/lib/foo.dart",
        "bytes_written": 42,
        "is_new_file": True,
    }
    executor = _make_executor_full(tmp_path, code_write=code_write)

    result = await executor.execute_tool(
        "write_project_code",
        {
            "relative_path": "example_app/lib/foo.dart",
            "content": "void main() {}",
            "reason": "3-1: add placeholder",
        },
    )
    assert result["path"] == "example_app/lib/foo.dart"
    code_write.write_source.assert_called_once()
    kwargs = code_write.write_source.call_args.kwargs
    assert kwargs["project_id"] == "proj-a"
    assert kwargs["reason"] == "3-1: add placeholder"


@pytest.mark.asyncio
async def test_git_commit_delegates_to_git_ops(tmp_path: Path):
    git_ops = MagicMock(spec=GitOpsService)
    git_ops.commit.return_value.to_dict.return_value = {
        "branch": "feature/3-1",
        "commit_sha": "abc12345",
        "files_count": 2,
        "message": "3-1: add DAO",
    }
    executor = _make_executor_full(tmp_path, git_ops=git_ops)

    result = await executor.execute_tool(
        "git_commit",
        {"message": "3-1: add DAO"},
    )
    assert result["branch"] == "feature/3-1"
    git_ops.commit.assert_called_once()


@pytest.mark.asyncio
async def test_write_role_artifact_dispatches_before_code_write(tmp_path: Path):
    executor = _make_executor_full(tmp_path)
    result = await executor.execute_tool(
        "write_role_artifact",
        {
            "path": "3-1-impl.md",
            "content": "# Implementation\nDid the thing.\n",
            "summary": "3-1 impl note",
        },
    )
    assert isinstance(result, dict)
    assert result.get("error") is None
    assert result["path"] == "3-1-impl.md"
    # File actually landed in the writer's root
    assert (tmp_path / "artifacts" / "3-1-impl.md").exists()


@pytest.mark.asyncio
async def test_unsupported_tool_raises(tmp_path: Path):
    executor = _make_executor_minimal(tmp_path)
    with pytest.raises(RuntimeError, match="Unsupported tool"):
        await executor.execute_tool("nonexistent", {})


# ---------------------------------------------------------------------------
# Thread-update hook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_update_fires_on_write(tmp_path: Path):
    updates: list[str] = []
    code_write = MagicMock(spec=CodeWriteService)
    code_write.write_source.return_value = {
        "path": "a.py",
        "bytes_written": 3,
        "is_new_file": True,
    }
    executor = DeveloperExecutor(
        sprint_state_service=_make_sprint(tmp_path),
        code_write_service=code_write,
        thread_update_fn=updates.append,
        project_id="proj-a",
    )
    await executor.execute_tool(
        "write_project_code",
        {"relative_path": "a.py", "content": "ok", "reason": "r"},
    )
    assert any("a.py" in u for u in updates)


def test_thread_update_failure_is_swallowed(tmp_path: Path):
    """Never let a broken thread update take the developer down."""
    def _boom(_line: str) -> None:
        raise RuntimeError("upstream feishu hiccup")

    executor = DeveloperExecutor(
        sprint_state_service=_make_sprint(tmp_path),
        thread_update_fn=_boom,
    )
    # Must not raise.
    executor._emit_code_write_update("hello")


# ---------------------------------------------------------------------------
# Frontmatter parity
# ---------------------------------------------------------------------------


def test_tool_allow_list_matches_role_file(tmp_path: Path):
    registry = RoleRegistryService(ROLES_DIR)
    role = registry.get_role("developer")
    executor = _make_executor_full(tmp_path)
    assert set(role.tool_allow_list) == {s.name for s in executor.tool_specs()}
