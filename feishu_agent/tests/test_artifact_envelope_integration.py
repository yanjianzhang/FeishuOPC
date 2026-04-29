"""Integration tests for the A-3 artifact envelope — T049.

Wires a real :class:`TechLeadToolExecutor` against a mocked LLM
sub-session and asserts that ``dispatch_role_agent``:

1. Writes a JSON file under
   ``{base}/teams/{root_trace_id}/artifacts/{role}-{artifact_id}.json``
   (SC-004-5).
2. Populates the envelope with the expected top-level fields:
   role, task, stop_reason, risk_score, tool_calls, token_usage,
   concurrency_group.
3. Materialises ``teams/{root}/inbox/`` as a side-effect (T046).
4. Emits a ``task_event_log`` ``artifact.write`` event when a
   ``task_handle`` is wired (T047 integration).
5. Covers the four post-dispatch exit paths: success, failure via
   result.success=False, ``TimeoutError`` bailout, and generic
   ``Exception`` bailout. Each must write its own artifact with
   the corresponding ``stop_reason``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
import yaml
from feishu_agent.core.llm_gateway_shim import MockGateway as _BaseMockGateway

from feishu_agent.core.llm_agent_adapter import LlmAgentAdapter, LlmSessionResult
from feishu_agent.roles.role_registry_service import RoleRegistryService
from feishu_agent.roles.tech_lead_executor import TechLeadToolExecutor
from feishu_agent.team.artifact_store import ArtifactStore, RoleArtifact
from feishu_agent.team.audit_service import AuditService
from feishu_agent.team.sprint_state_service import SprintStateService
from feishu_agent.team.task_event_log import TaskEventLog
from feishu_agent.team.task_service import TaskHandle, TaskKey
from feishu_agent.team.task_service import TaskMeta
from feishu_agent.tools.progress_sync_service import ProgressSyncService


class _HttpOnlyMockGateway(_BaseMockGateway):
    async def subscribe(self, event_types=None):
        raise NotImplementedError("HTTP-only path")


def _write_role(roles_dir: Path, name: str, *, allow: list[str]) -> None:
    fm = yaml.safe_dump({"tool_allow_list": allow}, default_flow_style=True).strip()
    (roles_dir / f"{name}.md").write_text(
        f"---\n{fm}\n---\nYou are the {name}.",
        encoding="utf-8",
    )


def _make_task_handle(tmp_path: Path) -> TaskHandle:
    """Build a real ``TaskHandle`` writing to a throwaway dir so we
    can assert against the on-disk events.jsonl. ``TaskService`` is
    too bulky for this test; we construct the primitives directly."""
    from datetime import datetime, timezone

    task_dir = tmp_path / "tasks" / "test_task"
    task_dir.mkdir(parents=True, exist_ok=True)
    log = TaskEventLog(task_dir)
    key = TaskKey(bot_name="tl", chat_id="chat", root_id="root")
    meta = TaskMeta(
        task_id=task_dir.name,
        bot_name=key.bot_name,
        chat_id=key.chat_id,
        root_id=key.root_id,
        role_name="tech_lead",
        project_id=None,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    return TaskHandle(key=key, log=log, meta=meta, lock=asyncio.Lock())


@pytest_asyncio.fixture()
async def tl_with_store(tmp_path: Path):
    """Build a TL wired with ArtifactStore + TaskHandle + a role
    registry carrying one harmless read-only role. Tests monkeypatch
    the LLM adapter's ``spawn_sub_agent`` to pick their desired
    outcome (success / failure / timeout / exception)."""

    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()
    _write_role(roles_dir, "repo_inspector", allow=["read_sprint_status"])

    run_log_dir = tmp_path / "techbot-runs"
    run_log_dir.mkdir()
    store = ArtifactStore(run_log_dir)

    mock_gw = _HttpOnlyMockGateway()
    mock_gw.register(
        "agents.create",
        lambda p: {"agentId": p.get("agentId", "test"), "status": "created"},
    )
    mock_gw.register(
        "chat.send",
        lambda p: {
            "runId": str(uuid.uuid4()),
            "content": "done",
            "status": "completed",
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        },
    )
    mock_gw.register("config.get", lambda p: {"agentId": "x", "tools": {}})
    mock_gw.register("config.set", lambda p: {"ok": True})
    mock_gw.register("config.patch", lambda p: {"ok": True})
    await mock_gw.connect()

    adapter = LlmAgentAdapter(
        gateway_url="ws://mock:18789/gateway",
        default_model="test",
        gateway=mock_gw,
        timeout=30,
    )
    await adapter.connect()

    status_file = "sprint-status.yaml"
    (tmp_path / status_file).write_text(
        yaml.safe_dump({"sprint_name": "S1", "current_sprint": {"goal": "x"}}),
        encoding="utf-8",
    )

    handle = _make_task_handle(tmp_path)
    root_trace = "rootTrace1"

    executor = TechLeadToolExecutor(
        progress_sync_service=MagicMock(spec=ProgressSyncService),
        sprint_state_service=SprintStateService(tmp_path, status_file),
        audit_service=AuditService(tmp_path / "audit"),
        llm_agent_adapter=adapter,
        role_registry=RoleRegistryService(roles_dir),
        project_id="proj",
        command_text="test",
        trace_id=root_trace,
        timeout_seconds=600,
        artifact_store=store,
        root_trace_id=root_trace,
        task_handle=handle,
    )
    yield executor, store, handle, root_trace


# ---------------------------------------------------------------------------
# Success path — artifact written with expected fields (SC-004-5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_writes_artifact_with_expected_fields(
    tl_with_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor, store, handle, root_trace = tl_with_store

    async def _fake_spawn(**kwargs: Any) -> LlmSessionResult:
        return LlmSessionResult(
            success=True,
            content="sub-agent completed OK",
            tool_calls=[],
            token_usage={"input": 42, "output": 17, "total_tokens": 59},
            latency_ms=50,
            error_message=None,
            stop_reason="complete",
        )

    monkeypatch.setattr(executor._llm_agent, "spawn_sub_agent", _fake_spawn)

    result = await executor.execute_tool(
        "dispatch_role_agent",
        {
            "role_name": "repo_inspector",
            "task": "Scan the repo for dead modules",
            "acceptance_criteria": "produce a sorted list",
        },
    )

    # LLM-visible return payload — new A-3 fields must be present.
    assert result["success"] is True
    assert isinstance(result["artifact_id"], str) and result["artifact_id"]
    assert result["artifact_path"] is not None

    # Exact on-disk path: teams/{root}/artifacts/{role}-{aid}.json.
    art_path = Path(result["artifact_path"])
    expected_dir = store.artifacts_dir(root_trace)
    assert art_path.parent == expected_dir
    assert art_path.exists()

    payload = json.loads(art_path.read_text(encoding="utf-8"))
    assert payload["role_name"] == "repo_inspector"
    assert payload["task"] == "Scan the repo for dead modules"
    assert payload["acceptance_criteria"] == "produce a sorted list"
    assert payload["success"] is True
    assert payload["stop_reason"] == "complete"
    assert payload["token_usage"]["total_tokens"] == 59
    assert payload["output_text"] == "sub-agent completed OK"
    assert payload["root_trace_id"] == root_trace
    assert payload["parent_trace_id"] == root_trace
    assert payload["concurrency_group"]
    # Risk score is a float in [0,1]; no tools called → 0.0.
    assert isinstance(payload["risk_score"], float)
    assert payload["risk_score"] == 0.0

    # T046 — inbox dir exists alongside artifacts.
    inbox = store.team_dir(root_trace) / "inbox"
    assert inbox.is_dir()

    # T047 — artifact.write event was appended to the task log.
    events = handle.events()
    artifact_events = [e for e in events if e.kind == "artifact.write"]
    assert len(artifact_events) == 1
    payload_evt = artifact_events[0].payload
    assert payload_evt["artifact_id"] == result["artifact_id"]
    assert payload_evt["role"] == "repo_inspector"
    assert payload_evt["success"] is True
    assert payload_evt["stop_reason"] == "complete"


# ---------------------------------------------------------------------------
# Failure (result.success=False) still writes an artifact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_result_writes_artifact(
    tl_with_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor, store, _handle, root_trace = tl_with_store

    async def _fake_spawn(**kwargs: Any) -> LlmSessionResult:
        return LlmSessionResult(
            success=False,
            content="",
            tool_calls=[],
            token_usage={"input": 10, "output": 0, "total_tokens": 10},
            latency_ms=20,
            error_message="LLM returned malformed tool call",
            stop_reason="error",
        )

    monkeypatch.setattr(executor._llm_agent, "spawn_sub_agent", _fake_spawn)

    result = await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "repo_inspector", "task": "broken task"},
    )

    assert result["success"] is False
    assert result["artifact_id"]
    art = store.read(root_trace, result["artifact_id"])
    assert art.success is False
    assert art.stop_reason == "error"
    assert art.error_message == "LLM returned malformed tool call"
    # failure contributes the 0.2 error_bonus even with no tool calls.
    assert art.risk_score == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# Timeout path — artifact still written with stop_reason=timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_writes_artifact(
    tl_with_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor, store, _handle, root_trace = tl_with_store

    async def _raise(**kwargs: Any) -> LlmSessionResult:
        raise TimeoutError("sub-agent timed out")

    monkeypatch.setattr(executor._llm_agent, "spawn_sub_agent", _raise)

    result = await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "repo_inspector", "task": "slow task"},
    )

    assert result["success"] is False
    assert result["error"] == "AGENT_TIMEOUT"
    assert result["artifact_id"]

    art = store.read(root_trace, result["artifact_id"])
    assert art.stop_reason == "timeout"
    assert art.success is False
    assert art.error_message == "AGENT_TIMEOUT"


# ---------------------------------------------------------------------------
# Generic exception — still writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exception_writes_artifact(
    tl_with_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor, store, _handle, root_trace = tl_with_store

    async def _boom(**kwargs: Any) -> LlmSessionResult:
        raise RuntimeError("gateway exploded")

    monkeypatch.setattr(executor._llm_agent, "spawn_sub_agent", _boom)

    result = await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "repo_inspector", "task": "bad gateway"},
    )

    assert result["success"] is False
    assert "gateway exploded" in result["error"]
    assert result["artifact_id"]

    art = store.read(root_trace, result["artifact_id"])
    assert art.stop_reason == "error"
    assert art.success is False


# ---------------------------------------------------------------------------
# Pre-dispatch bailouts — NO artifact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_role_writes_no_artifact(tl_with_store) -> None:
    executor, store, _handle, root_trace = tl_with_store

    result = await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "not_a_real_role", "task": "whatever"},
    )

    assert result["success"] is False
    assert "UNKNOWN_ROLE" in result["error"]
    assert "artifact_id" not in result  # pre-dispatch guard, no envelope
    # And the artifacts dir is either absent or empty.
    art_dir = store.team_dir(root_trace) / "artifacts"
    assert not art_dir.exists() or list(art_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# Replay integration — TeamReplay.from_trace folds events + artifacts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_team_replay_from_trace(
    tl_with_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T048: ``TeamReplay.from_trace`` returns an object with both
    the event snapshot and the artifact list, and ``format_lines``
    produces human-readable output for every artifact."""

    from feishu_agent.team.task_replay import TeamReplay

    executor, store, handle, root_trace = tl_with_store

    async def _ok(**kwargs: Any) -> LlmSessionResult:
        return LlmSessionResult(
            success=True, content="ok", tool_calls=[], token_usage={},
            latency_ms=1, error_message=None, stop_reason="complete",
        )

    monkeypatch.setattr(executor._llm_agent, "spawn_sub_agent", _ok)

    # Fire two dispatches so the team has >1 artifact to replay.
    for t in ("first", "second"):
        await executor.execute_tool(
            "dispatch_role_agent",
            {"role_name": "repo_inspector", "task": t},
        )

    replay = TeamReplay.from_trace(
        root_trace,
        events=handle.events(),
        store=store,
    )
    assert len(replay.artifacts) == 2
    assert all(isinstance(a, RoleArtifact) for a in replay.artifacts)
    # Both artifact.write events landed in the snapshot's counts.
    assert replay.snapshot.event_counts.get("artifact.write") == 2

    lines = replay.format_lines()
    # Each artifact produces at least one "header" line with role
    # and stop_reason; empty tool_calls means just that one line.
    header_lines = [ln for ln in lines if ln.startswith("[repo_inspector]")]
    assert len(header_lines) == 2
    assert all("stop=complete" in ln for ln in header_lines)
