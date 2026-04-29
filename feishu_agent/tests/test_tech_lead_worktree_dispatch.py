"""Story 004.5 — AC-1/AC-2/AC-3/AC-6/AC-8 integration tests.

These tests pin the TL-dispatch wiring added for story 004.5:

* AC-1: ``WorktreeManager.acquire`` is called before the sub-session spawns
  (for roles with ``needs_worktree=True``), and the sub-executor is
  built with ``working_dir == handle.path``.
* AC-2: ``release`` fires in every exit path (success / failure / timeout
  / exception), with the right ``success`` flag; a ``worktree.release``
  audit event is emitted in each case.
* AC-3: ``RoleArtifact.worktree_fallback`` records the effective value
  (``False`` for a real worktree, ``True`` for a fallback handle).
* AC-6: With the ``WorktreeManager`` disabled, two dispatches BOTH get
  fallback handles; artifact flag shows ``True`` on both.
* AC-8: ``dispatch_role_agent`` with an explicit ``task_id`` claims +
  completes the task on success; on failure it releases back to
  ``pending``; an already-claimed task yields a ``claim_warning`` in
  the result payload WITHOUT aborting the dispatch.

These tests do NOT require real git — ``WorktreeManager`` is injected
with a stubbed git runner or replaced outright. The real-git side is
covered by ``test_worktree_concurrent_commits.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from feishu_agent.core.agent_types import AgentToolExecutor, AgentToolSpec
from feishu_agent.core.llm_agent_adapter import LlmAgentAdapter
from feishu_agent.roles.role_registry_service import (
    RoleDefinition,
    RoleRegistryService,
)
from feishu_agent.roles.tech_lead_executor import TechLeadToolExecutor
from feishu_agent.team.artifact_store import ArtifactStore
from feishu_agent.team.audit_service import AuditService
from feishu_agent.team.sprint_state_service import SprintStateService
from feishu_agent.team.task_graph import TaskGraph
from feishu_agent.team.worktree_manager import (
    WorktreeHandle,
    WorktreeManager,
)
from feishu_agent.tools.progress_sync_service import ProgressSyncService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_role(
    tmp_path: Path,
    role_name: str,
    *,
    needs_worktree: bool,
    worktree_base_branch: str = "main",
) -> None:
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir(exist_ok=True)
    body = (
        f"---\n"
        f"role_name: {role_name}\n"
        f"system_prompt: role for tests\n"
        f"needs_worktree: {str(needs_worktree).lower()}\n"
        f"worktree_base_branch: {worktree_base_branch}\n"
        f"---\n"
    )
    (roles_dir / f"{role_name}.md").write_text(body, encoding="utf-8")


class _StubWorktreeManager:
    """In-process stand-in for ``WorktreeManager`` that never calls
    git. Records every acquire/release so we can assert on the wiring
    without the real-git overhead."""

    def __init__(
        self,
        repo_root: Path,
        *,
        enabled: bool = True,
        force_fallback: bool = False,
    ) -> None:
        self._repo_root = repo_root.resolve()
        self._enabled = enabled
        self._force_fallback = force_fallback
        self.acquire_calls: list[tuple[str, str]] = []
        self.release_calls: list[tuple[str, bool, bool]] = []

    def acquire(
        self, child_trace_id: str, base_branch: str = "main"
    ) -> WorktreeHandle:
        self.acquire_calls.append((child_trace_id, base_branch))
        if not self._enabled or self._force_fallback:
            return WorktreeHandle(
                path=self._repo_root,
                branch=base_branch,
                child_trace_id=child_trace_id,
                base_branch=base_branch,
                created_at=0,
                repo_root=self._repo_root,
            )
        wt_path = self._repo_root / ".worktrees" / child_trace_id
        wt_path.mkdir(parents=True, exist_ok=True)
        return WorktreeHandle(
            path=wt_path.resolve(),
            branch=f"agent/{child_trace_id}",
            child_trace_id=child_trace_id,
            base_branch=base_branch,
            created_at=0,
            repo_root=self._repo_root,
        )

    def release(
        self,
        handle: WorktreeHandle,
        *,
        keep_on_failure: bool = True,
        success: bool = True,
    ) -> bool:
        self.release_calls.append(
            (handle.child_trace_id, success, handle.is_fallback)
        )
        if handle.is_fallback:
            return False
        if not success and keep_on_failure:
            return False
        return True


class _RecordingAudit(AuditService):
    """AuditService that stores emitted events in a list for
    assertions. Subclassing keeps the TL's isinstance-style usage
    unchanged (we want to exercise the real code path)."""

    def __init__(self, audit_dir: Path) -> None:
        super().__init__(audit_dir)
        self.events: list[tuple[str, dict[str, Any]]] = []

    def record(self, event_kind: str, payload: dict[str, Any]) -> None:  # type: ignore[override]
        self.events.append((event_kind, dict(payload)))
        # intentionally skip file I/O — tests only care about the in-memory log.


def _make_executor(
    tmp_path: Path,
    *,
    worktree_manager: Any | None,
    task_graph: TaskGraph | None = None,
    audit: AuditService | None = None,
    sprint_state: SprintStateService | None = None,
    artifact_store: ArtifactStore | None = None,
    role_executor_provider: Any | None = None,
    timeout_seconds: int = 600,
) -> TechLeadToolExecutor:
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir(exist_ok=True)
    audit_dir = tmp_path / "audit"
    mock_sync = MagicMock(spec=ProgressSyncService)
    mock_sync.repo_root = tmp_path
    mock_llm = MagicMock(spec=LlmAgentAdapter)
    sprint = sprint_state or SprintStateService(tmp_path, "sprint-status.yaml")
    return TechLeadToolExecutor(
        progress_sync_service=mock_sync,
        sprint_state_service=sprint,
        audit_service=audit or AuditService(audit_dir),
        llm_agent_adapter=mock_llm,
        role_registry=RoleRegistryService(roles_dir),
        project_id="p",
        command_text="c",
        trace_id="parent",
        chat_id="chat",
        timeout_seconds=timeout_seconds,
        worktree_manager=worktree_manager,
        task_graph=task_graph,
        artifact_store=artifact_store,
        role_executor_provider=role_executor_provider,
    )


class _StubSubExecutor(AgentToolExecutor):
    """Minimal ``AgentToolExecutor`` stub: returns an empty tool set
    and never runs anything. Good enough for tests that only care
    about whether the provider was called with the right kwargs —
    ``spawn_sub_agent_with_tools`` is mocked by the caller."""

    def tool_specs(self) -> list[AgentToolSpec]:  # type: ignore[override]
        return []

    async def execute_tool(  # type: ignore[override]
        self, name: str, args: dict[str, Any]
    ) -> Any:
        return None


class _SpyProvider:
    """Story 004.5 H1 — records every (role_name, role, kwargs) the
    TL hands it, so we can assert ``working_dir=handle.path`` is
    threaded through. Keeping a separate ``_provider_accepts_working_dir``
    introspection-visible signature: an explicit ``working_dir``
    keyword-only parameter so ``inspect.signature`` picks it up.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, RoleDefinition, dict[str, Any]]] = []

    def __call__(
        self,
        role_name: str,
        role: RoleDefinition,
        *,
        working_dir: Path | None = None,
    ) -> AgentToolExecutor:
        self.calls.append(
            (role_name, role, {"working_dir": working_dir})
        )
        return _StubSubExecutor()


class _LegacyProvider:
    """H3 regression fixture — the pre-004.5 two-arg provider. Used
    to prove that the TL introspection path does NOT pass
    ``working_dir`` to a provider that doesn't declare it.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, RoleDefinition]] = []

    def __call__(
        self, role_name: str, role: RoleDefinition
    ) -> AgentToolExecutor:
        self.calls.append((role_name, role))
        return _StubSubExecutor()


# ---------------------------------------------------------------------------
# AC-1 / AC-2 / AC-3 — worktree acquire + release + artifact flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_acquires_and_releases_worktree_on_success(
    tmp_path: Path,
) -> None:
    _write_role(tmp_path, "developer", needs_worktree=True)
    wt_mgr = _StubWorktreeManager(tmp_path)
    audit = _RecordingAudit(tmp_path / "audit")
    executor = _make_executor(
        tmp_path, worktree_manager=wt_mgr, audit=audit
    )

    result = MagicMock()
    result.success = True
    result.content = "done"
    result.error_message = None
    result.latency_ms = 10
    result.stop_reason = "complete"
    result.token_usage = {}
    executor._llm_agent.spawn_sub_agent = AsyncMock(return_value=result)
    executor._llm_agent.spawn_sub_agent_with_tools = AsyncMock(
        return_value=result
    )

    payload = await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "developer", "task": "build a widget"},
    )

    assert payload["success"] is True
    assert len(wt_mgr.acquire_calls) == 1, "acquire must fire once on dispatch"
    child_trace_id, base_branch = wt_mgr.acquire_calls[0]
    assert base_branch == "main"
    assert child_trace_id.startswith("parent-")

    # AC-2 — release fires with success=True on the happy path.
    assert len(wt_mgr.release_calls) == 1
    assert wt_mgr.release_calls[0][0] == child_trace_id
    assert wt_mgr.release_calls[0][1] is True  # success flag
    assert wt_mgr.release_calls[0][2] is False  # not fallback

    # AC-2 — both audit events emitted in order.
    kinds = [k for k, _ in audit.events]
    assert "worktree.acquire" in kinds
    assert "worktree.release" in kinds
    # release event payload mirrors what operators grep for.
    rel_event = next(p for k, p in audit.events if k == "worktree.release")
    assert rel_event["success"] is True
    assert rel_event["fallback"] is False
    assert rel_event["child_trace_id"] == child_trace_id


@pytest.mark.asyncio
async def test_dispatch_releases_worktree_with_success_false_on_failure(
    tmp_path: Path,
) -> None:
    _write_role(tmp_path, "developer", needs_worktree=True)
    wt_mgr = _StubWorktreeManager(tmp_path)
    audit = _RecordingAudit(tmp_path / "audit")
    executor = _make_executor(
        tmp_path, worktree_manager=wt_mgr, audit=audit
    )

    result = MagicMock()
    result.success = False
    result.content = ""
    result.error_message = "role raised"
    result.latency_ms = 10
    result.stop_reason = "error"
    result.token_usage = {}
    executor._llm_agent.spawn_sub_agent = AsyncMock(return_value=result)
    executor._llm_agent.spawn_sub_agent_with_tools = AsyncMock(
        return_value=result
    )

    payload = await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "developer", "task": "fail"},
    )
    assert payload["success"] is False
    assert wt_mgr.release_calls == [
        (wt_mgr.acquire_calls[0][0], False, False)
    ], "release must be invoked with success=False"
    # Audit event reflects the failure + retention decision.
    rel = next(p for k, p in audit.events if k == "worktree.release")
    assert rel["success"] is False
    assert rel["kept_on_failure"] is True


@pytest.mark.asyncio
async def test_dispatch_releases_worktree_on_timeout(tmp_path: Path) -> None:
    _write_role(tmp_path, "developer", needs_worktree=True)
    wt_mgr = _StubWorktreeManager(tmp_path)
    executor = _make_executor(tmp_path, worktree_manager=wt_mgr)
    executor._llm_agent.spawn_sub_agent = AsyncMock(
        side_effect=TimeoutError("slow")
    )
    executor._llm_agent.spawn_sub_agent_with_tools = AsyncMock(
        side_effect=TimeoutError("slow")
    )

    payload = await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "developer", "task": "timeout test"},
    )
    assert payload["error"] == "AGENT_TIMEOUT"
    assert len(wt_mgr.release_calls) == 1
    assert wt_mgr.release_calls[0][1] is False  # success=False


@pytest.mark.asyncio
async def test_dispatch_releases_worktree_on_unexpected_exception(
    tmp_path: Path,
) -> None:
    _write_role(tmp_path, "developer", needs_worktree=True)
    wt_mgr = _StubWorktreeManager(tmp_path)
    executor = _make_executor(tmp_path, worktree_manager=wt_mgr)
    executor._llm_agent.spawn_sub_agent = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    executor._llm_agent.spawn_sub_agent_with_tools = AsyncMock(
        side_effect=RuntimeError("boom")
    )

    payload = await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "developer", "task": "exception test"},
    )
    assert payload["success"] is False
    assert payload["error"] == "boom"
    assert len(wt_mgr.release_calls) == 1
    assert wt_mgr.release_calls[0][1] is False


@pytest.mark.asyncio
async def test_dispatch_skips_worktree_when_role_does_not_need_one(
    tmp_path: Path,
) -> None:
    """AC-3 specifies: roles with ``needs_worktree=False`` receive
    ``working_dir == repo_root`` unchanged. That means
    ``worktree_manager.acquire`` is NEVER called for them."""
    _write_role(tmp_path, "repo_inspector", needs_worktree=False)
    wt_mgr = _StubWorktreeManager(tmp_path)
    executor = _make_executor(tmp_path, worktree_manager=wt_mgr)
    result = MagicMock()
    result.success = True
    result.content = "ok"
    result.error_message = None
    result.latency_ms = 1
    result.stop_reason = "complete"
    result.token_usage = {}
    executor._llm_agent.spawn_sub_agent = AsyncMock(return_value=result)
    executor._llm_agent.spawn_sub_agent_with_tools = AsyncMock(
        return_value=result
    )

    await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "repo_inspector", "task": "inspect"},
    )
    assert wt_mgr.acquire_calls == [], "manager must be untouched for needs_worktree=False"
    assert wt_mgr.release_calls == []


# ---------------------------------------------------------------------------
# AC-6 — fallback-flag propagation when manager is disabled
# ---------------------------------------------------------------------------


def test_worktree_manager_disabled_returns_fallback_handle(tmp_path: Path) -> None:
    """Unit-level half of AC-6: ``WorktreeManager(enabled=False)`` →
    every call returns a fallback handle pointing at repo_root."""
    (tmp_path / ".git").mkdir()
    mgr = WorktreeManager(tmp_path, enabled=False)
    a = mgr.acquire("trace-a")
    b = mgr.acquire("trace-b")
    assert a.is_fallback is True
    assert b.is_fallback is True
    assert a.path.resolve() == tmp_path.resolve()
    assert b.path.resolve() == tmp_path.resolve()


@pytest.mark.asyncio
async def test_dispatch_with_disabled_manager_emits_fallback_release(
    tmp_path: Path,
) -> None:
    """Integration half of AC-6: a ``needs_worktree=True`` role
    running under a disabled manager still gets a
    ``worktree.release`` event (with ``fallback=True``) — the
    operator's grep sees a complete lifecycle regardless of the
    isolation flag's value."""
    _write_role(tmp_path, "developer", needs_worktree=True)
    wt_mgr = _StubWorktreeManager(tmp_path, enabled=False)
    audit = _RecordingAudit(tmp_path / "audit")
    executor = _make_executor(
        tmp_path, worktree_manager=wt_mgr, audit=audit
    )
    result = MagicMock()
    result.success = True
    result.content = "ok"
    result.error_message = None
    result.latency_ms = 1
    result.stop_reason = "complete"
    result.token_usage = {}
    executor._llm_agent.spawn_sub_agent = AsyncMock(return_value=result)
    executor._llm_agent.spawn_sub_agent_with_tools = AsyncMock(
        return_value=result
    )

    await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "developer", "task": "t"},
    )

    acquire_event = next(p for k, p in audit.events if k == "worktree.acquire")
    release_event = next(p for k, p in audit.events if k == "worktree.release")
    assert acquire_event["fallback"] is True
    assert release_event["fallback"] is True


# ---------------------------------------------------------------------------
# AC-8 — TaskGraph claim wiring
# ---------------------------------------------------------------------------


def _make_sprint_with_task(
    tmp_path: Path,
    task_id: str = "T999",
) -> SprintStateService:
    status_file = "sprint-status.yaml"
    (tmp_path / status_file).write_text(
        yaml.safe_dump(
            {
                "development_status": {},
                "tasks": [
                    {
                        "id": task_id,
                        "status": "pending",
                        "blockedBy": [],
                        "blocks": [],
                    }
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return SprintStateService(tmp_path, status_file)


@pytest.mark.asyncio
async def test_dispatch_with_task_id_claims_and_completes_on_success(
    tmp_path: Path,
) -> None:
    _write_role(tmp_path, "developer", needs_worktree=False)
    sprint = _make_sprint_with_task(tmp_path, "T100")
    tg = TaskGraph(sprint)
    executor = _make_executor(
        tmp_path,
        worktree_manager=None,
        task_graph=tg,
        sprint_state=sprint,
    )
    result = MagicMock()
    result.success = True
    result.content = "ok"
    result.error_message = None
    result.latency_ms = 1
    result.stop_reason = "complete"
    result.token_usage = {}
    executor._llm_agent.spawn_sub_agent = AsyncMock(return_value=result)
    executor._llm_agent.spawn_sub_agent_with_tools = AsyncMock(
        return_value=result
    )

    payload = await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "developer", "task": "t", "task_id": "T100"},
    )
    assert payload["success"] is True
    assert "claim_warning" not in payload
    task = tg.get("T100")
    assert task.status == "done"
    assert task.claim is None


@pytest.mark.asyncio
async def test_dispatch_with_task_id_releases_claim_on_failure(
    tmp_path: Path,
) -> None:
    _write_role(tmp_path, "developer", needs_worktree=False)
    sprint = _make_sprint_with_task(tmp_path, "T200")
    tg = TaskGraph(sprint)
    executor = _make_executor(
        tmp_path,
        worktree_manager=None,
        task_graph=tg,
        sprint_state=sprint,
    )
    result = MagicMock()
    result.success = False
    result.content = ""
    result.error_message = "fail"
    result.latency_ms = 1
    result.stop_reason = "error"
    result.token_usage = {}
    executor._llm_agent.spawn_sub_agent = AsyncMock(return_value=result)
    executor._llm_agent.spawn_sub_agent_with_tools = AsyncMock(
        return_value=result
    )

    await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "developer", "task": "t", "task_id": "T200"},
    )
    task = tg.get("T200")
    # Task was released back to pending (no longer claimed).
    assert task.status == "pending"
    assert task.claim is None


@pytest.mark.asyncio
async def test_dispatch_with_already_claimed_task_warns_and_proceeds(
    tmp_path: Path,
) -> None:
    _write_role(tmp_path, "developer", needs_worktree=False)
    sprint = _make_sprint_with_task(tmp_path, "T300")
    tg = TaskGraph(sprint)
    # Pre-claim the task under a different trace so the TL's own
    # claim call hits a conflict.
    tg.claim("T300", "another-trace", ttl_seconds=3600)

    executor = _make_executor(
        tmp_path,
        worktree_manager=None,
        task_graph=tg,
        sprint_state=sprint,
    )
    result = MagicMock()
    result.success = True
    result.content = "ok"
    result.error_message = None
    result.latency_ms = 1
    result.stop_reason = "complete"
    result.token_usage = {}
    executor._llm_agent.spawn_sub_agent = AsyncMock(return_value=result)
    executor._llm_agent.spawn_sub_agent_with_tools = AsyncMock(
        return_value=result
    )

    payload = await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "developer", "task": "t", "task_id": "T300"},
    )
    assert payload["success"] is True, (
        "claim conflict must NOT abort dispatch — the LLM still "
        "gets to do its work without the claim."
    )
    assert "claim_warning" in payload
    assert "T300" in payload["claim_warning"]
    task = tg.get("T300")
    # The pre-existing claim still stands.
    assert task.claim is not None
    assert task.claim.trace_id == "another-trace"


@pytest.mark.asyncio
async def test_dispatch_without_task_id_skips_claim_entirely(
    tmp_path: Path,
) -> None:
    """Sanity — the default path (no task_id) must not touch
    TaskGraph at all. A pre-existing task stays untouched."""
    _write_role(tmp_path, "developer", needs_worktree=False)
    sprint = _make_sprint_with_task(tmp_path, "T400")
    tg = TaskGraph(sprint)
    executor = _make_executor(
        tmp_path,
        worktree_manager=None,
        task_graph=tg,
        sprint_state=sprint,
    )
    result = MagicMock()
    result.success = True
    result.content = "ok"
    result.error_message = None
    result.latency_ms = 1
    result.stop_reason = "complete"
    result.token_usage = {}
    executor._llm_agent.spawn_sub_agent = AsyncMock(return_value=result)
    executor._llm_agent.spawn_sub_agent_with_tools = AsyncMock(
        return_value=result
    )

    await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "developer", "task": "t"},
    )
    task = tg.get("T400")
    assert task.status == "pending"
    assert task.claim is None


# ---------------------------------------------------------------------------
# H1 — Provider wiring: working_dir=handle.path is actually threaded
# through to role_executor_provider. AC-1 "set BundleContext.working_dir
# = handle.path" has no other proof in the test suite.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_passes_worktree_path_to_provider(
    tmp_path: Path,
) -> None:
    """AC-1 end-to-end: the provider MUST receive
    ``working_dir=handle.path`` (NOT repo_root) when the role needs
    a worktree and the handle is non-fallback."""
    _write_role(tmp_path, "developer", needs_worktree=True)
    wt_mgr = _StubWorktreeManager(tmp_path)
    spy = _SpyProvider()
    executor = _make_executor(
        tmp_path,
        worktree_manager=wt_mgr,
        role_executor_provider=spy,
    )
    result = MagicMock()
    result.success = True
    result.content = "ok"
    result.error_message = None
    result.latency_ms = 1
    result.stop_reason = "complete"
    result.token_usage = {}
    executor._llm_agent.spawn_sub_agent_with_tools = AsyncMock(
        return_value=result
    )

    await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "developer", "task": "t"},
    )

    assert len(spy.calls) == 1, "provider must be called exactly once"
    role_name, _role, kwargs = spy.calls[0]
    assert role_name == "developer"
    # The stub manager returns ``repo_root/.worktrees/<trace>``; that's
    # the path we expect the provider to receive.
    handle = wt_mgr.acquire_calls[0]
    expected_path = (tmp_path / ".worktrees" / handle[0]).resolve()
    assert kwargs["working_dir"] == expected_path
    assert kwargs["working_dir"] != tmp_path.resolve()


@pytest.mark.asyncio
async def test_dispatch_does_not_override_working_dir_for_needs_worktree_false(
    tmp_path: Path,
) -> None:
    """AC-3 invariant: ``needs_worktree=False`` → provider receives
    ``working_dir=None`` (its default), so ``BundleContext.working_dir``
    stays at ``repo_root``."""
    _write_role(tmp_path, "repo_inspector", needs_worktree=False)
    wt_mgr = _StubWorktreeManager(tmp_path)
    spy = _SpyProvider()
    executor = _make_executor(
        tmp_path,
        worktree_manager=wt_mgr,
        role_executor_provider=spy,
    )
    result = MagicMock()
    result.success = True
    result.content = "ok"
    result.error_message = None
    result.latency_ms = 1
    result.stop_reason = "complete"
    result.token_usage = {}
    executor._llm_agent.spawn_sub_agent_with_tools = AsyncMock(
        return_value=result
    )

    await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "repo_inspector", "task": "t"},
    )
    assert len(spy.calls) == 1
    assert spy.calls[0][2]["working_dir"] is None


@pytest.mark.asyncio
async def test_dispatch_does_not_override_working_dir_for_fallback_handle(
    tmp_path: Path,
) -> None:
    """AC-1 clarifier: a fallback handle (path == repo_root) must NOT
    cause ``working_dir`` to be set on the provider call — the override
    would signal an intent the provider can't honour and confuses
    the `BundleContext.working_dir != repo_root` invariant documented
    at ``feishu_agent/tools/bundle_context.py``."""
    _write_role(tmp_path, "developer", needs_worktree=True)
    wt_mgr = _StubWorktreeManager(tmp_path, force_fallback=True)
    spy = _SpyProvider()
    executor = _make_executor(
        tmp_path,
        worktree_manager=wt_mgr,
        role_executor_provider=spy,
    )
    result = MagicMock()
    result.success = True
    result.content = "ok"
    result.error_message = None
    result.latency_ms = 1
    result.stop_reason = "complete"
    result.token_usage = {}
    executor._llm_agent.spawn_sub_agent_with_tools = AsyncMock(
        return_value=result
    )

    await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "developer", "task": "t"},
    )
    assert len(spy.calls) == 1
    assert spy.calls[0][2]["working_dir"] is None


# ---------------------------------------------------------------------------
# H3 — legacy two-arg providers are detected by signature introspection;
# working_dir is NOT passed to them, and no TypeError-catch-all hides
# unrelated bugs.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_omits_working_dir_for_legacy_two_arg_provider(
    tmp_path: Path,
) -> None:
    _write_role(tmp_path, "developer", needs_worktree=True)
    wt_mgr = _StubWorktreeManager(tmp_path)
    legacy = _LegacyProvider()
    executor = _make_executor(
        tmp_path,
        worktree_manager=wt_mgr,
        role_executor_provider=legacy,
    )
    result = MagicMock()
    result.success = True
    result.content = "ok"
    result.error_message = None
    result.latency_ms = 1
    result.stop_reason = "complete"
    result.token_usage = {}
    executor._llm_agent.spawn_sub_agent_with_tools = AsyncMock(
        return_value=result
    )

    await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "developer", "task": "t"},
    )
    # Legacy provider was invoked exactly once with the positional args,
    # NOT re-invoked under a TypeError fallback.
    assert len(legacy.calls) == 1
    # Worktree was still acquired + released — isolation still happens
    # at the WorktreeManager layer even though BundleContext can't
    # receive the override.
    assert len(wt_mgr.acquire_calls) == 1
    assert len(wt_mgr.release_calls) == 1


@pytest.mark.asyncio
async def test_provider_type_error_is_not_silently_swallowed(
    tmp_path: Path,
) -> None:
    """H3 regression: a provider body that raises TypeError for
    unrelated reasons must surface as ``sub_executor=None`` (logged
    via the generic exception branch) — NOT silently retried
    without ``working_dir``. The whole point of replacing the
    broad ``except TypeError`` with signature introspection is to
    stop hiding these bugs.
    """
    _write_role(tmp_path, "developer", needs_worktree=True)
    wt_mgr = _StubWorktreeManager(tmp_path)

    retry_count = {"n": 0}

    def buggy_provider(
        role_name: str,
        role: RoleDefinition,
        *,
        working_dir: Path | None = None,
    ) -> AgentToolExecutor:
        retry_count["n"] += 1
        # Simulate an unrelated bug: raise TypeError EVEN when called
        # with the expected signature.
        raise TypeError("unrelated bug inside provider body")

    executor = _make_executor(
        tmp_path,
        worktree_manager=wt_mgr,
        role_executor_provider=buggy_provider,
    )
    result = MagicMock()
    result.success = True
    result.content = "ok"
    result.error_message = None
    result.latency_ms = 1
    result.stop_reason = "complete"
    result.token_usage = {}
    executor._llm_agent.spawn_sub_agent = AsyncMock(return_value=result)
    executor._llm_agent.spawn_sub_agent_with_tools = AsyncMock(
        return_value=result
    )

    await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "developer", "task": "t"},
    )
    # Provider invoked exactly ONCE — not re-invoked under a catch-all.
    assert retry_count["n"] == 1, (
        "provider must not be retried after an unrelated TypeError; "
        "the pre-fix code retried once without working_dir and silently "
        "lost worktree isolation"
    )


# ---------------------------------------------------------------------------
# H2 — AC-6 full coverage: TWO dispatches under a disabled manager,
# artifact-store read-back, release events with fallback=True.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_dispatches_under_disabled_manager_both_record_fallback(
    tmp_path: Path,
) -> None:
    """AC-6 verbatim — dispatch 2 ``needs_worktree=True`` roles with a
    disabled manager; both MUST:
      - see a fallback handle (is_fallback=True) on acquire,
      - emit paired acquire/release audit events with ``fallback=True``,
      - have ``RoleArtifact.worktree_fallback == True`` persisted.
    """
    _write_role(tmp_path, "developer", needs_worktree=True)
    _write_role(tmp_path, "bug_fixer", needs_worktree=True)
    wt_mgr = _StubWorktreeManager(tmp_path, enabled=False)
    audit = _RecordingAudit(tmp_path / "audit")
    store = ArtifactStore(tmp_path / "store")
    executor = _make_executor(
        tmp_path,
        worktree_manager=wt_mgr,
        audit=audit,
        artifact_store=store,
    )
    result = MagicMock()
    result.success = True
    result.content = "ok"
    result.error_message = None
    result.latency_ms = 1
    result.stop_reason = "complete"
    result.token_usage = {}
    executor._llm_agent.spawn_sub_agent = AsyncMock(return_value=result)
    executor._llm_agent.spawn_sub_agent_with_tools = AsyncMock(
        return_value=result
    )

    await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "developer", "task": "t1"},
    )
    await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "bug_fixer", "task": "t2"},
    )

    # Both dispatches acquired + released a handle.
    assert len(wt_mgr.acquire_calls) == 2
    assert len(wt_mgr.release_calls) == 2
    # Both release-call entries report fallback=True (third tuple field).
    assert all(rc[2] is True for rc in wt_mgr.release_calls)

    # Audit stream shows 2 acquire + 2 release events, all flagged
    # ``fallback=True`` — this is the invariant the runbook tells
    # operators to grep for.
    acquires = [p for k, p in audit.events if k == "worktree.acquire"]
    releases = [p for k, p in audit.events if k == "worktree.release"]
    assert len(acquires) == 2 and len(releases) == 2
    assert all(e["fallback"] is True for e in acquires)
    assert all(e["fallback"] is True for e in releases)

    # AC-6 canonical check: the persisted artifacts carry
    # ``worktree_fallback=True``. This is the surface the LLM sees
    # in a follow-up dispatch, not just an in-memory flag.
    artifact_files = sorted(
        (tmp_path / "store" / "teams" / "parent" / "artifacts").glob("*.json")
    )
    assert len(artifact_files) == 2, (
        f"expected 2 artifacts written, got {len(artifact_files)}"
    )
    for af in artifact_files:
        data = json.loads(af.read_text(encoding="utf-8"))
        assert data["worktree_fallback"] is True, (
            f"artifact {af.name} must record worktree_fallback=True, "
            f"got {data.get('worktree_fallback')!r}"
        )


# ---------------------------------------------------------------------------
# M1 — pin the claim TTL derivation. AC-8 says ``ttl_seconds=sub_timeout``;
# ``sub_timeout = remaining_budget - reserve`` but the TL skip-below-min
# branch (``MIN_SUB_AGENT_TIMEOUT_SECONDS``, 120s) guarantees the claim
# path only ever sees TTLs at or above that minimum. Anchoring the test
# to the real invariant means a future refactor that accidentally
# bypasses the skip-branch — or drops the TTL to something dangerous
# like 5s — gets caught by CI.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_ttl_derives_from_sub_timeout(
    tmp_path: Path,
) -> None:
    _write_role(tmp_path, "developer", needs_worktree=False)
    sprint = _make_sprint_with_task(tmp_path, "T500")
    tg = TaskGraph(sprint)

    ttl_seen: list[int] = []
    original_claim = tg.claim

    def spy_claim(task_id: str, trace: str, *, ttl_seconds: int) -> Any:
        ttl_seen.append(ttl_seconds)
        return original_claim(task_id, trace, ttl_seconds=ttl_seconds)

    tg.claim = spy_claim  # type: ignore[assignment]

    # timeout_seconds=600 (our default) → sub_timeout ≈ 600 - 30 reserve.
    executor = _make_executor(
        tmp_path,
        worktree_manager=None,
        task_graph=tg,
        sprint_state=sprint,
        timeout_seconds=600,
    )
    result = MagicMock()
    result.success = True
    result.content = "ok"
    result.error_message = None
    result.latency_ms = 1
    result.stop_reason = "complete"
    result.token_usage = {}
    executor._llm_agent.spawn_sub_agent = AsyncMock(return_value=result)
    executor._llm_agent.spawn_sub_agent_with_tools = AsyncMock(
        return_value=result
    )

    await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "developer", "task": "t", "task_id": "T500"},
    )
    assert ttl_seen, "TaskGraph.claim must have been called"
    # From tech_lead_executor: MIN_SUB_AGENT_TIMEOUT_SECONDS = 120.
    # Dispatches below that short-circuit with OUT_OF_BUDGET before
    # claiming, so any TTL we DO see must be at least 120.
    assert 120 <= ttl_seen[0] <= 600, (
        f"ttl_seconds must derive from sub_timeout (120..600 given a "
        f"600s budget with a 30s reserve); got {ttl_seen[0]}"
    )


# ---------------------------------------------------------------------------
# M3 — claim.skipped audit event on claim conflict / not-found paths.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_conflict_emits_claim_skipped_audit_event(
    tmp_path: Path,
) -> None:
    _write_role(tmp_path, "developer", needs_worktree=False)
    sprint = _make_sprint_with_task(tmp_path, "T600")
    tg = TaskGraph(sprint)
    tg.claim("T600", "another-trace", ttl_seconds=3600)

    audit = _RecordingAudit(tmp_path / "audit")
    executor = _make_executor(
        tmp_path,
        worktree_manager=None,
        task_graph=tg,
        audit=audit,
        sprint_state=sprint,
    )
    result = MagicMock()
    result.success = True
    result.content = "ok"
    result.error_message = None
    result.latency_ms = 1
    result.stop_reason = "complete"
    result.token_usage = {}
    executor._llm_agent.spawn_sub_agent = AsyncMock(return_value=result)
    executor._llm_agent.spawn_sub_agent_with_tools = AsyncMock(
        return_value=result
    )

    await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "developer", "task": "t", "task_id": "T600"},
    )
    skipped = [p for k, p in audit.events if k == "claim.skipped"]
    assert len(skipped) == 1
    assert skipped[0]["task_id"] == "T600"
    assert skipped[0]["reason"] == "conflict"


@pytest.mark.asyncio
async def test_claim_not_found_emits_claim_skipped_audit_event(
    tmp_path: Path,
) -> None:
    _write_role(tmp_path, "developer", needs_worktree=False)
    # Empty sprint-status — any task_id is "not found".
    sprint = _make_sprint_with_task(tmp_path, "T_OTHER")
    tg = TaskGraph(sprint)

    audit = _RecordingAudit(tmp_path / "audit")
    executor = _make_executor(
        tmp_path,
        worktree_manager=None,
        task_graph=tg,
        audit=audit,
        sprint_state=sprint,
    )
    result = MagicMock()
    result.success = True
    result.content = "ok"
    result.error_message = None
    result.latency_ms = 1
    result.stop_reason = "complete"
    result.token_usage = {}
    executor._llm_agent.spawn_sub_agent = AsyncMock(return_value=result)
    executor._llm_agent.spawn_sub_agent_with_tools = AsyncMock(
        return_value=result
    )

    await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "developer", "task": "t", "task_id": "T_MISSING"},
    )
    skipped = [p for k, p in audit.events if k == "claim.skipped"]
    assert len(skipped) == 1
    assert skipped[0]["task_id"] == "T_MISSING"
    assert skipped[0]["reason"] == "not_found"
