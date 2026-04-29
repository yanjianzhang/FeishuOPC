"""Spec / wiring tests for every shipped tool bundle.

These tests are deliberately shape-oriented: they check that each
bundle ships the tool names / effects / targets its role frontmatter
will declare, and that handlers route through the backing service
stub. Service behavior itself is covered by the existing per-service
tests (``test_git_ops_service.py``, ``test_progress_sync_service``, …);
re-validating that here would duplicate coverage and couple the
bundle tests to service internals.

A few sanity dispatches (``_dispatch_*``) exercise the closure-capture
path so a regression like "handler forgot to bind ctx" fails loudly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from feishu_agent.tools.bundle_context import BundleContext
from feishu_agent.tools.bundle_registry import BundleRegistry
from feishu_agent.tools.bundles import register_builtin_bundles
from feishu_agent.tools.bundles.bitable_read import build_bitable_read_bundle
from feishu_agent.tools.bundles.bitable_write import build_bitable_write_bundle
from feishu_agent.tools.bundles.feishu_chat import build_feishu_chat_bundle
from feishu_agent.tools.bundles.fs_read import build_fs_read_bundle
from feishu_agent.tools.bundles.fs_write import build_fs_write_bundle
from feishu_agent.tools.bundles.git_local import build_git_local_bundle
from feishu_agent.tools.bundles.git_remote import build_git_remote_bundle
from feishu_agent.tools.bundles.search import build_search_bundle
from feishu_agent.tools.bundles.sprint import build_sprint_bundle

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _empty_ctx(tmp_path: Path, **overrides: Any) -> BundleContext:
    kwargs: dict[str, Any] = {
        "working_dir": tmp_path,
        "repo_root": tmp_path,
        "chat_id": "chat-1",
        "trace_id": "trace-1",
        "role_name": "test_role",
        "project_id": "test-project",
    }
    kwargs.update(overrides)
    return BundleContext(**kwargs)


# ---------------------------------------------------------------------------
# register_builtin_bundles
# ---------------------------------------------------------------------------


def test_register_builtin_bundles_registers_nine_bundles(tmp_path: Path) -> None:
    registry = BundleRegistry()
    register_builtin_bundles(registry)
    assert set(registry.known_bundles()) == {
        "sprint",
        "bitable_read",
        "bitable_write",
        "fs_read",
        "fs_write",
        "git_local",
        "git_remote",
        "search",
        "feishu_chat",
    }


def test_register_builtin_bundles_is_single_call(tmp_path: Path) -> None:
    registry = BundleRegistry()
    register_builtin_bundles(registry)
    with pytest.raises(ValueError):
        register_builtin_bundles(registry)


# ---------------------------------------------------------------------------
# sprint bundle
# ---------------------------------------------------------------------------


def test_sprint_bundle_empty_without_service(tmp_path: Path) -> None:
    assert build_sprint_bundle(_empty_ctx(tmp_path)) == []


def test_sprint_bundle_exposes_two_tools_with_correct_metadata(
    tmp_path: Path,
) -> None:
    sprint = MagicMock()
    sprint.load_status_data.return_value = {
        "sprint_name": "sprint-1",
        "current_sprint": {"goal": "ship A-2"},
    }
    ctx = _empty_ctx(tmp_path, sprint_service=sprint)
    items = build_sprint_bundle(ctx)
    names = [spec.name for spec, _ in items]
    assert names == ["read_sprint_status", "advance_sprint_state"]

    read_spec, read_handler = items[0]
    assert read_spec.effect == "read"
    assert read_spec.target == "read.sprint"
    result = read_handler({"sprint": "current"})
    assert result["sprint_name"] == "sprint-1"
    assert result["goal"] == "ship A-2"

    advance_spec, _advance_handler = items[1]
    assert advance_spec.effect == "self"
    assert advance_spec.target == "self.sprint"


def test_sprint_bundle_advance_delegates_to_service(tmp_path: Path) -> None:
    sprint = MagicMock()
    # Mimic the shape the current advance() path returns.
    change = MagicMock()
    change.story_key = "3-1"
    change.from_status = "planned"
    change.to_status = "in-progress"
    change.model_dump.return_value = {"story_key": "3-1"}
    sprint.advance.return_value = [change]

    ctx = _empty_ctx(tmp_path, sprint_service=sprint, command_text="go")
    _read, advance = (h for _, h in build_sprint_bundle(ctx))
    payload = advance({"story_key": "3-1", "to_status": "in-progress"})
    assert payload["story_key"] == "3-1"
    assert payload["from_status"] == "planned"
    assert payload["to_status"] == "in-progress"
    assert sprint.advance.call_args.kwargs["reason"].startswith("test_role")


# ---------------------------------------------------------------------------
# bitable_read / bitable_write
# ---------------------------------------------------------------------------


def test_bitable_read_bundle_empty_without_service(tmp_path: Path) -> None:
    assert build_bitable_read_bundle(_empty_ctx(tmp_path)) == []


def test_bitable_read_bundle_specs(tmp_path: Path) -> None:
    ctx = _empty_ctx(tmp_path, progress_sync_service=MagicMock())
    items = build_bitable_read_bundle(ctx)
    names = [spec.name for spec, _ in items]
    assert names == [
        "read_bitable_schema",
        "read_bitable_rows",
        "resolve_bitable_target",
    ]
    for spec, _ in items:
        assert spec.effect == "read"
        assert spec.target == "read.bitable"


def test_bitable_write_bundle_specs(tmp_path: Path) -> None:
    ctx = _empty_ctx(tmp_path, progress_sync_service=MagicMock())
    items = build_bitable_write_bundle(ctx)
    names = [spec.name for spec, _ in items]
    assert names == ["preview_progress_sync", "write_progress_sync"]
    preview_spec, _ = items[0]
    write_spec, _ = items[1]
    assert preview_spec.effect == "read"
    assert preview_spec.target == "read.bitable"
    assert write_spec.effect == "world"
    assert write_spec.target == "world.bitable"


# ---------------------------------------------------------------------------
# fs_read / fs_write
# ---------------------------------------------------------------------------


def test_fs_read_bundle_empty_without_service(tmp_path: Path) -> None:
    assert build_fs_read_bundle(_empty_ctx(tmp_path)) == []


def test_fs_read_bundle_exposes_read_surface(tmp_path: Path) -> None:
    code_write = MagicMock()
    code_write.describe_policy.return_value = {"policy": "ok"}
    code_write.read_source.return_value = {"content": "abc"}
    code_write.list_paths.return_value = {"entries": []}
    ctx = _empty_ctx(tmp_path, code_write_service=code_write)
    items = build_fs_read_bundle(ctx)
    names = [spec.name for spec, _ in items]
    assert set(names) == {
        "describe_code_write_policy",
        "read_project_code",
        "list_project_paths",
    }
    for spec, _ in items:
        assert spec.effect == "read"
        assert spec.target == "read.fs"


def test_fs_read_bundle_dispatches_to_service(tmp_path: Path) -> None:
    code_write = MagicMock()
    code_write.read_source.return_value = {"content": "hello"}
    ctx = _empty_ctx(tmp_path, code_write_service=code_write)
    items = {spec.name: handler for spec, handler in build_fs_read_bundle(ctx)}
    result = items["read_project_code"](
        {"relative_path": "a.py", "max_bytes": 100}
    )
    assert result == {"content": "hello"}
    code_write.read_source.assert_called_once()


def test_fs_write_bundle_omits_code_tools_without_code_service(
    tmp_path: Path,
) -> None:
    ctx = _empty_ctx(tmp_path)
    items = build_fs_write_bundle(ctx)
    assert items == []


def test_fs_write_bundle_with_artifact_writer_exposes_artifact_tools(
    tmp_path: Path,
) -> None:
    # A minimal RoleArtifactWriter stub with the shape real executors
    # see: .tool_specs() → list, .try_handle(name, args) → dict | None.
    writer = _FakeArtifactWriter(tmp_path)
    ctx = _empty_ctx(tmp_path, role_artifact_writer=writer)
    items = build_fs_write_bundle(ctx)
    names = {spec.name for spec, _ in items}
    assert "write_role_artifact" in names
    # write_file is intentionally NOT in fs_write: prd_writer's scope
    # differs from RoleArtifactWriter._allowed_root — see module
    # docstring for the rationale.
    assert "write_file" not in names
    for spec, _ in items:
        # Role-artifact writes are classified "self" (agent-own output
        # channel) so roles with allow_effects=[read, self] /
        # allow_targets=["read.*", "self.*"] keep the tool after
        # BundleRegistry filtering.
        if spec.name == "write_role_artifact":
            assert spec.effect == "self"
            assert spec.target == "self.artifact"


# ---------------------------------------------------------------------------
# git_local / git_remote
# ---------------------------------------------------------------------------


def test_git_local_bundle_empty_without_service(tmp_path: Path) -> None:
    assert build_git_local_bundle(_empty_ctx(tmp_path)) == []


def test_git_local_bundle_exposes_commit_only(tmp_path: Path) -> None:
    git_ops = MagicMock()
    ctx = _empty_ctx(tmp_path, git_ops_service=git_ops)
    items = build_git_local_bundle(ctx)
    names = [spec.name for spec, _ in items]
    assert names == ["git_commit"]
    spec, _ = items[0]
    assert spec.effect == "world"
    assert spec.target == "world.git.local"


def test_git_remote_bundle_empty_without_services(tmp_path: Path) -> None:
    assert build_git_remote_bundle(_empty_ctx(tmp_path)) == []


def test_git_remote_bundle_partial_wiring(tmp_path: Path) -> None:
    git_ops = MagicMock()
    ctx = _empty_ctx(tmp_path, git_ops_service=git_ops)
    items = build_git_remote_bundle(ctx)
    names = {spec.name for spec, _ in items}
    assert names == {"git_push", "git_sync_remote"}


def test_git_remote_bundle_full_wiring(tmp_path: Path) -> None:
    git_ops = MagicMock()
    pr = MagicMock()
    ctx = _empty_ctx(
        tmp_path, git_ops_service=git_ops, pull_request_service=pr
    )
    items = build_git_remote_bundle(ctx)
    names = {spec.name for spec, _ in items}
    assert names == {"git_push", "git_sync_remote", "create_pull_request"}
    for spec, _ in items:
        assert spec.effect == "world"
        assert spec.target == "world.git.remote"


# ---------------------------------------------------------------------------
# search bundle
# ---------------------------------------------------------------------------


def test_search_bundle_empty_without_workflow(tmp_path: Path) -> None:
    assert build_search_bundle(_empty_ctx(tmp_path)) == []


def test_search_bundle_exposes_workflow_read_tools(tmp_path: Path) -> None:
    workflow = MagicMock()
    ctx = _empty_ctx(tmp_path, workflow_service=workflow)
    items = build_search_bundle(ctx)
    names = [spec.name for spec, _ in items]
    assert set(names) == {
        "read_workflow_instruction",
        "list_workflow_artifacts",
        "read_repo_file",
    }
    for spec, _ in items:
        assert spec.effect == "read"
        assert spec.target.startswith("read.")


# ---------------------------------------------------------------------------
# feishu_chat bundle (empty placeholder)
# ---------------------------------------------------------------------------


def test_feishu_chat_bundle_is_empty_placeholder(tmp_path: Path) -> None:
    assert build_feishu_chat_bundle(_empty_ctx(tmp_path)) == []


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeArtifactWriter:
    root: Path

    @property
    def _allowed_root(self) -> Path:
        return self.root

    def tool_specs(self) -> list[Any]:
        # Import inside to avoid circular deps at module load.
        from feishu_agent.team.role_artifact_writer import (
            ROLE_ARTIFACT_TOOL_SPECS,
        )

        return list(ROLE_ARTIFACT_TOOL_SPECS)

    def try_handle(self, _tool: str, _args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}
