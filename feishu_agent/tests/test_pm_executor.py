from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
import yaml
from feishu_agent.core.llm_gateway_shim import MockGateway as _BaseMockGateway

from feishu_agent.core.llm_agent_adapter import LlmAgentAdapter
from feishu_agent.roles.pm_executor import (
    PM_TOOL_SPECS,
    PMToolExecutor,
)
from feishu_agent.roles.role_executors import register_role_executors
from feishu_agent.roles.role_registry_service import RoleRegistryService
from feishu_agent.runtime.managed_feishu_client import ManagedFeishuClient


class HttpOnlyMockGateway(_BaseMockGateway):
    async def subscribe(self, event_types=None):
        raise NotImplementedError("Forces HTTP-only execute path")


def _build_chat_response(content: str = "ok") -> dict:
    return {
        "runId": str(uuid.uuid4()),
        "content": content,
        "status": "completed",
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    }


def _write_role_file(roles_dir: Path, name: str, *, tags: list[str], tool_allow_list: list[str], body: str) -> None:
    fm = yaml.safe_dump({"tags": tags, "tool_allow_list": tool_allow_list}, default_flow_style=True).strip()
    (roles_dir / f"{name}.md").write_text(f"---\n{fm}\n---\n{body}", encoding="utf-8")


@pytest_asyncio.fixture()
async def pm_setup(tmp_path: Path):
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()
    _write_role_file(
        roles_dir, "prd_writer",
        tags=["plan"], tool_allow_list=["write_file"],
        body="You are the PRD Writer.",
    )
    _write_role_file(
        roles_dir, "researcher",
        tags=["brainstorm", "plan"], tool_allow_list=["read_bitable_rows", "read_bitable_schema"],
        body="You are the Researcher.",
    )

    mock_gw = HttpOnlyMockGateway()
    mock_gw.register("agents.create", lambda p: {"agentId": p.get("agentId", "test"), "status": "created"})
    mock_gw.register("chat.send", lambda p: _build_chat_response("PRD generated successfully"))
    mock_gw.register("config.get", lambda p: {"agentId": "test", "tools": {}})
    mock_gw.register("config.set", lambda p: {"ok": True})
    mock_gw.register("config.patch", lambda p: {"ok": True})
    await mock_gw.connect()

    adapter = LlmAgentAdapter(
        gateway_url="ws://mock:18789/gateway",
        default_model="doubao-seed-2-0-pro-260215",
        gateway=mock_gw,
        timeout=30,
    )
    await adapter.connect()

    registry = RoleRegistryService(roles_dir)
    register_role_executors(registry)

    mock_feishu = MagicMock(spec=ManagedFeishuClient)
    mock_feishu.request = AsyncMock(return_value={"message_id": "msg-001"})

    executor = PMToolExecutor(
        llm_agent_adapter=adapter,
        role_registry=registry,
        feishu_client=mock_feishu,
        notify_chat_id="oc_test_chat_id",
        timeout_seconds=30,
    )
    return executor, mock_feishu


# ======================================================================
# tool_specs()
# ======================================================================


def test_tool_specs_returns_exactly_2_tools():
    executor = PMToolExecutor(
        llm_agent_adapter=MagicMock(spec=LlmAgentAdapter),
        role_registry=MagicMock(spec=RoleRegistryService),
    )
    specs = executor.tool_specs()
    assert len(specs) == 2


def test_tool_specs_names():
    executor = PMToolExecutor(
        llm_agent_adapter=MagicMock(spec=LlmAgentAdapter),
        role_registry=MagicMock(spec=RoleRegistryService),
    )
    names = {s.name for s in executor.tool_specs()}
    assert names == {"dispatch_role_agent", "notify_tech_lead"}


def test_tool_specs_returns_fresh_list():
    executor = PMToolExecutor(
        llm_agent_adapter=MagicMock(spec=LlmAgentAdapter),
        role_registry=MagicMock(spec=RoleRegistryService),
    )
    a = executor.tool_specs()
    b = executor.tool_specs()
    assert a is not b
    assert a == b


def test_module_level_specs_match_instance():
    executor = PMToolExecutor(
        llm_agent_adapter=MagicMock(spec=LlmAgentAdapter),
        role_registry=MagicMock(spec=RoleRegistryService),
    )
    assert [s.name for s in executor.tool_specs()] == [s.name for s in PM_TOOL_SPECS]


# ======================================================================
# dispatch_role_agent
# ======================================================================


@pytest.mark.asyncio
async def test_dispatch_role_agent_happy_path(pm_setup):
    executor, _ = pm_setup
    result = await executor.execute_tool("dispatch_role_agent", {
        "role_name": "prd_writer",
        "task": "Write a PRD for the notification feature",
    })
    assert result["success"] is True
    assert result["role_name"] == "prd_writer"
    assert isinstance(result["output"], str)
    assert result["error"] is None
    assert isinstance(result["latency_ms"], int)


@pytest.mark.asyncio
async def test_dispatch_unknown_role(pm_setup):
    executor, _ = pm_setup
    result = await executor.execute_tool("dispatch_role_agent", {
        "role_name": "nonexistent_role",
        "task": "Do something",
    })
    assert result["success"] is False
    assert "UNKNOWN_ROLE" in result["error"]


# ======================================================================
# notify_tech_lead
# ======================================================================


@pytest.mark.asyncio
async def test_notify_tech_lead_happy_path(pm_setup):
    executor, mock_feishu = pm_setup
    msg_text = "PRD 已生成：用户通知功能设计文档。请查看并安排技术评估。"
    result = await executor.execute_tool("notify_tech_lead", {
        "message": msg_text,
    })
    assert result["sent"] is True
    assert result["chat_id"] == "oc_test_chat_id"
    assert result["message_id"] == "msg-001"
    assert result["error"] is None

    mock_feishu.request.assert_awaited_once()
    call_args = mock_feishu.request.call_args
    assert call_args[0][0] == "POST"
    assert "/im/v1/messages" in call_args[0][1]
    assert "receive_id_type=chat_id" in call_args[0][1]

    import json as _json
    body = call_args[1]["json_body"]
    assert body["receive_id"] == "oc_test_chat_id"
    assert body["msg_type"] == "text"
    assert _json.loads(body["content"])["text"] == msg_text


@pytest.mark.asyncio
async def test_notify_tech_lead_no_chat_id():
    executor = PMToolExecutor(
        llm_agent_adapter=MagicMock(spec=LlmAgentAdapter),
        role_registry=MagicMock(spec=RoleRegistryService),
        feishu_client=MagicMock(spec=ManagedFeishuClient),
        notify_chat_id=None,
    )
    result = await executor.execute_tool("notify_tech_lead", {
        "message": "Test",
    })
    assert result["sent"] is False
    assert "not configured" in result["error"]


@pytest.mark.asyncio
async def test_notify_tech_lead_no_feishu_client():
    executor = PMToolExecutor(
        llm_agent_adapter=MagicMock(spec=LlmAgentAdapter),
        role_registry=MagicMock(spec=RoleRegistryService),
        feishu_client=None,
        notify_chat_id="oc_test",
    )
    result = await executor.execute_tool("notify_tech_lead", {
        "message": "Test",
    })
    assert result["sent"] is False
    assert "not available" in result["error"]


@pytest.mark.asyncio
async def test_notify_tech_lead_feishu_api_error(pm_setup):
    executor, mock_feishu = pm_setup
    mock_feishu.request = AsyncMock(side_effect=RuntimeError("Feishu API 500"))
    result = await executor.execute_tool("notify_tech_lead", {
        "message": "Test",
    })
    assert result["sent"] is False
    assert "500" in result["error"]


# ======================================================================
# Unsupported tool
# ======================================================================


@pytest.mark.asyncio
async def test_unsupported_tool_raises(pm_setup):
    executor, _ = pm_setup
    with pytest.raises(RuntimeError, match="Unsupported tool"):
        await executor.execute_tool("nonexistent", {})


# ======================================================================
# run_speckit_script surface (PM only — TL gets a different whitelist)
# ======================================================================


from feishu_agent.tools.speckit_script_service import (  # noqa: E402
    SpeckitScriptError,
    SpeckitScriptService,
)


class _StubSpeckitService(SpeckitScriptService):
    """Records calls so we can assert the executor wires (agent_name,
    project_id, script, args) correctly without spawning bash."""

    def __init__(self, *, raise_with: SpeckitScriptError | None = None):
        super().__init__(project_roots={"proj-x": Path("/tmp/proj-x")})
        self.calls: list[dict] = []
        self._raise = raise_with

    def run_script(self, *, agent_name, project_id, script, args=None):
        self.calls.append({
            "agent_name": agent_name,
            "project_id": project_id,
            "script": script,
            "args": list(args or []),
        })
        if self._raise is not None:
            raise self._raise
        from feishu_agent.tools.speckit_script_service import SpeckitScriptResult
        return SpeckitScriptResult(
            script=script,
            argv=tuple(args or []),
            exit_code=0,
            success=True,
            stdout='{"BRANCH_NAME":"003-foo","SPEC_FILE":"specs/003-foo/spec.md","FEATURE_NUM":"003"}\n',
            stderr="",
            parsed_json={
                "BRANCH_NAME": "003-foo",
                "SPEC_FILE": "specs/003-foo/spec.md",
                "FEATURE_NUM": "003",
            },
            elapsed_ms=42,
        )


def test_speckit_tool_absent_when_service_not_wired():
    """Default constructor (no speckit_script_service) → no tool spec.
    Keeps the prompt clean for projects without ``.specify/``."""

    executor = PMToolExecutor(
        llm_agent_adapter=MagicMock(spec=LlmAgentAdapter),
        role_registry=MagicMock(spec=RoleRegistryService),
    )
    names = {s.name for s in executor.tool_specs()}
    assert "run_speckit_script" not in names


def test_speckit_tool_present_when_service_wired():
    executor = PMToolExecutor(
        llm_agent_adapter=MagicMock(spec=LlmAgentAdapter),
        role_registry=MagicMock(spec=RoleRegistryService),
        speckit_script_service=_StubSpeckitService(),
        project_id="proj-x",
    )
    names = {s.name for s in executor.tool_specs()}
    assert "run_speckit_script" in names


@pytest.mark.asyncio
async def test_run_speckit_script_dispatches_with_pm_agent_name():
    stub = _StubSpeckitService()
    executor = PMToolExecutor(
        llm_agent_adapter=MagicMock(spec=LlmAgentAdapter),
        role_registry=MagicMock(spec=RoleRegistryService),
        speckit_script_service=stub,
        project_id="proj-x",
    )
    result = await executor.execute_tool(
        "run_speckit_script",
        {
            "script": "create-new-feature.sh",
            "args": ["--json", "--short-name", "user-auth", "Add user auth"],
        },
    )
    assert result["success"] is True
    assert result["parsed_json"]["BRANCH_NAME"] == "003-foo"
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["agent_name"] == "product_manager"
    assert call["project_id"] == "proj-x"
    assert call["script"] == "create-new-feature.sh"
    assert call["args"] == ["--json", "--short-name", "user-auth", "Add user auth"]


@pytest.mark.asyncio
async def test_run_speckit_script_translates_service_errors_to_tool_payload():
    from feishu_agent.tools.speckit_script_service import ScriptNotAllowedError

    stub = _StubSpeckitService(
        raise_with=ScriptNotAllowedError("not allowed for this role"),
    )
    executor = PMToolExecutor(
        llm_agent_adapter=MagicMock(spec=LlmAgentAdapter),
        role_registry=MagicMock(spec=RoleRegistryService),
        speckit_script_service=stub,
        project_id="proj-x",
    )
    result = await executor.execute_tool(
        "run_speckit_script",
        {"script": "setup-plan.sh"},
    )
    assert result["error"] == "SCRIPT_NOT_ALLOWED_FOR_AGENT"
    assert "not allowed" in result["message"]


@pytest.mark.asyncio
async def test_run_speckit_script_without_service_returns_typed_error():
    """If the LLM somehow calls the tool when the service was not wired
    (e.g. stale tool advertised by another bot), we return a typed
    error, not a 500."""

    executor = PMToolExecutor(
        llm_agent_adapter=MagicMock(spec=LlmAgentAdapter),
        role_registry=MagicMock(spec=RoleRegistryService),
    )
    result = await executor.execute_tool(
        "run_speckit_script",
        {"script": "create-new-feature.sh"},
    )
    assert result["error"] == "SPECKIT_SCRIPT_SERVICE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_run_speckit_script_bad_args_payload():
    stub = _StubSpeckitService()
    executor = PMToolExecutor(
        llm_agent_adapter=MagicMock(spec=LlmAgentAdapter),
        role_registry=MagicMock(spec=RoleRegistryService),
        speckit_script_service=stub,
        project_id="proj-x",
    )
    result = await executor.execute_tool(
        "run_speckit_script",
        {"args": ["--json"]},
    )
    assert result["error"] == "SPECKIT_SCRIPT_BAD_ARGS"
    assert stub.calls == []  # never reached the service


# ======================================================================
# publish_artifacts surface (PM only — TL uses GitOpsService for code)
# ======================================================================


from feishu_agent.team.artifact_publish_service import (  # noqa: E402
    ArtifactPublishError,
    ArtifactPublishService,
    PublishResult,
)


class _StubArtifactPublishService(ArtifactPublishService):
    """Records calls without actually running git. Mirrors
    ``_StubSpeckitService`` style. Stays is_agent_enabled==True for
    ``product_manager`` because the parent class reads the module-level
    agent allow-list — which we deliberately don't touch from tests."""

    def __init__(self, *, raise_with: ArtifactPublishError | None = None):
        super().__init__(project_roots={"proj-x": Path("/tmp/proj-x")})
        self.calls: list[dict] = []
        self._raise = raise_with

    def publish(
        self,
        *,
        agent_name,
        project_id,
        relative_paths,
        commit_message,
        remote="origin",
    ):
        self.calls.append(
            {
                "agent_name": agent_name,
                "project_id": project_id,
                "relative_paths": list(relative_paths),
                "commit_message": commit_message,
                "remote": remote,
            }
        )
        if self._raise is not None:
            raise self._raise
        return PublishResult(
            project_id=project_id,
            agent_name=agent_name,
            branch="004-foo",
            commit_sha="abc123",
            commit_message=commit_message,
            paths=tuple(relative_paths),
            remote=remote,
            pushed=True,
            push_output="To origin\n",
            elapsed_ms=17,
        )


def test_publish_tool_absent_when_service_not_wired():
    executor = PMToolExecutor(
        llm_agent_adapter=MagicMock(spec=LlmAgentAdapter),
        role_registry=MagicMock(spec=RoleRegistryService),
    )
    names = {s.name for s in executor.tool_specs()}
    assert "publish_artifacts" not in names


def test_publish_tool_present_when_service_wired():
    executor = PMToolExecutor(
        llm_agent_adapter=MagicMock(spec=LlmAgentAdapter),
        role_registry=MagicMock(spec=RoleRegistryService),
        artifact_publish_service=_StubArtifactPublishService(),
        project_id="proj-x",
    )
    names = {s.name for s in executor.tool_specs()}
    assert "publish_artifacts" in names


@pytest.mark.asyncio
async def test_publish_artifacts_dispatches_with_pm_agent_name():
    stub = _StubArtifactPublishService()
    executor = PMToolExecutor(
        llm_agent_adapter=MagicMock(spec=LlmAgentAdapter),
        role_registry=MagicMock(spec=RoleRegistryService),
        artifact_publish_service=stub,
        project_id="proj-x",
    )
    result = await executor.execute_tool(
        "publish_artifacts",
        {
            "relative_paths": ["specs/004-foo/spec.md"],
            "commit_message": "spec: foo initial",
        },
    )
    assert result["pushed"] is True
    assert result["branch"] == "004-foo"
    assert result["commit_sha"] == "abc123"
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["agent_name"] == "product_manager"
    assert call["project_id"] == "proj-x"
    assert call["relative_paths"] == ["specs/004-foo/spec.md"]
    assert call["commit_message"] == "spec: foo initial"
    assert call["remote"] == "origin"


@pytest.mark.asyncio
async def test_publish_artifacts_translates_service_errors_to_tool_payload():
    from feishu_agent.team.artifact_publish_service import (
        ExtraStagedFilesError,
    )

    stub = _StubArtifactPublishService(
        raise_with=ExtraStagedFilesError("index has foo.txt"),
    )
    executor = PMToolExecutor(
        llm_agent_adapter=MagicMock(spec=LlmAgentAdapter),
        role_registry=MagicMock(spec=RoleRegistryService),
        artifact_publish_service=stub,
        project_id="proj-x",
    )
    result = await executor.execute_tool(
        "publish_artifacts",
        {
            "relative_paths": ["specs/foo.md"],
            "commit_message": "spec: foo",
        },
    )
    assert result["error"] == "EXTRA_STAGED_FILES"
    assert "index has foo.txt" in result["message"]


@pytest.mark.asyncio
async def test_publish_artifacts_without_service_returns_typed_error():
    executor = PMToolExecutor(
        llm_agent_adapter=MagicMock(spec=LlmAgentAdapter),
        role_registry=MagicMock(spec=RoleRegistryService),
    )
    result = await executor.execute_tool(
        "publish_artifacts",
        {
            "relative_paths": ["specs/foo.md"],
            "commit_message": "m",
        },
    )
    assert result["error"] == "ARTIFACT_PUBLISH_UNAVAILABLE"


@pytest.mark.asyncio
async def test_publish_artifacts_bad_args_payload():
    stub = _StubArtifactPublishService()
    executor = PMToolExecutor(
        llm_agent_adapter=MagicMock(spec=LlmAgentAdapter),
        role_registry=MagicMock(spec=RoleRegistryService),
        artifact_publish_service=stub,
        project_id="proj-x",
    )
    # Missing commit_message
    result = await executor.execute_tool(
        "publish_artifacts",
        {"relative_paths": ["specs/foo.md"]},
    )
    assert result["error"] == "ARTIFACT_PUBLISH_BAD_ARGS"
    assert stub.calls == []
