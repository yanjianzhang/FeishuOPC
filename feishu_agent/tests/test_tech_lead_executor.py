from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from feishu_agent.core.llm_agent_adapter import LlmAgentAdapter
from feishu_agent.roles.role_registry_service import RoleRegistryService
from feishu_agent.roles.tech_lead_executor import (
    TECH_LEAD_V2_TOOL_SPECS,
    TechLeadToolExecutor,
)
from feishu_agent.runtime.managed_feishu_client import ManagedFeishuClient
from feishu_agent.team.audit_service import AuditService
from feishu_agent.team.sprint_state_service import SprintStateService
from feishu_agent.tools.feishu_agent_tools import AdvanceSprintStateArgs
from feishu_agent.tools.progress_sync_service import ProgressSyncService

EXPECTED_TOOL_NAMES = {
    "read_sprint_status",
    "advance_sprint_state",
    "dispatch_role_agent",
    "request_confirmation",
    "delegate_to_application_agent",
}


def _make_executor(
    tmp_path: Path,
    *,
    status_data: dict[str, Any] | None = None,
) -> TechLeadToolExecutor:
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()
    audit_dir = tmp_path / "audit"
    status_file = "sprint-status.yaml"
    status_path = tmp_path / status_file
    if status_data:
        status_path.write_text(yaml.safe_dump(status_data, allow_unicode=True), encoding="utf-8")

    mock_sync = MagicMock(spec=ProgressSyncService)
    mock_sync.repo_root = tmp_path
    mock_llm = MagicMock(spec=LlmAgentAdapter)

    return TechLeadToolExecutor(
        progress_sync_service=mock_sync,
        sprint_state_service=SprintStateService(tmp_path, status_file),
        audit_service=AuditService(audit_dir),
        llm_agent_adapter=mock_llm,
        role_registry=RoleRegistryService(roles_dir),
        project_id="test-project",
        command_text="test command",
        trace_id="trace-001",
        chat_id="chat-001",
    )


# ======================================================================
# T021 — tool_specs() tests
# ======================================================================


def test_tool_specs_returns_exactly_5_tools(tmp_path: Path):
    executor = _make_executor(tmp_path)
    specs = executor.tool_specs()
    assert len(specs) == 5


def test_tool_specs_names_match_expected_set(tmp_path: Path):
    executor = _make_executor(tmp_path)
    specs = executor.tool_specs()
    names = {s.name for s in specs}
    assert names == EXPECTED_TOOL_NAMES


def test_dispatch_role_agent_schema_has_required_fields(tmp_path: Path):
    executor = _make_executor(tmp_path)
    specs = executor.tool_specs()
    dispatch_spec = next(s for s in specs if s.name == "dispatch_role_agent")
    props = dispatch_spec.input_schema.get("properties", {})
    assert "role_name" in props
    assert "task" in props
    assert "acceptance_criteria" in props


def test_tool_specs_returns_fresh_list(tmp_path: Path):
    """tool_specs() returns a new list each call (no shared mutable state)."""
    executor = _make_executor(tmp_path)
    a = executor.tool_specs()
    b = executor.tool_specs()
    assert a is not b
    assert a == b


def test_module_level_specs_match_instance_specs(tmp_path: Path):
    """Module-level spec list is the superset; instance filters out
    append_agent_note when no notes service is wired and
    resume_last_dispatch when no TaskHandle is wired.

    The module constant is the static, fully-populated menu — used
    by doc generators and tests that don't care about runtime wiring.
    Deploy tools no longer live on the tech lead; they are owned by
    the ``deploy_engineer`` role and covered by
    ``test_deploy_engineer_executor.py``.
    """
    executor = _make_executor(tmp_path)
    module_names = {s.name for s in TECH_LEAD_V2_TOOL_SPECS}
    assert "append_agent_note" in module_names
    assert "resume_last_dispatch" in module_names
    # Deploy tools were extracted to deploy_engineer; TL must not
    # advertise them anymore.
    assert "deploy_project" not in module_names
    assert "describe_deploy_project" not in module_names
    instance_names = {s.name for s in executor.tool_specs()}
    assert "append_agent_note" not in instance_names
    assert "resume_last_dispatch" not in instance_names


def test_append_agent_note_tool_appears_when_service_wired(tmp_path: Path):
    from feishu_agent.team.agent_notes_service import AgentNotesService

    project_root = tmp_path / "project"
    project_root.mkdir()
    notes = AgentNotesService(
        project_id="demo", project_root=project_root
    )
    executor = _make_executor(tmp_path)
    executor._agent_notes = notes

    names = {s.name for s in executor.tool_specs()}
    assert "append_agent_note" in names


@pytest.mark.asyncio
async def test_append_agent_note_happy_path(tmp_path: Path):
    from feishu_agent.team.agent_notes_service import AgentNotesService

    project_root = tmp_path / "project"
    project_root.mkdir()
    notes = AgentNotesService(
        project_id="demo", project_root=project_root
    )
    executor = _make_executor(tmp_path)
    executor._agent_notes = notes

    result = await executor.execute_tool(
        "append_agent_note",
        {"note": "always rebuild docs after schema change"},
    )
    assert result["stored"] is True
    assert result["project_id"] == "demo"
    # File exists + contains the note text
    assert (project_root / "AGENT_NOTES.md").exists()
    assert "always rebuild docs" in (project_root / "AGENT_NOTES.md").read_text()


@pytest.mark.asyncio
async def test_append_agent_note_returns_error_when_service_missing(tmp_path: Path):
    executor = _make_executor(tmp_path)
    # _agent_notes stays None by default (the _make_executor fixture
    # doesn't wire it). Calling the tool in that state should surface
    # an LLM-friendly error, not raise.
    result = await executor.execute_tool(
        "append_agent_note", {"note": "anything"}
    )
    assert result["stored"] is False
    assert result["error"] == "AGENT_NOTE_DISABLED"


@pytest.mark.asyncio
async def test_append_agent_note_oversize_returns_code(tmp_path: Path):
    from feishu_agent.team.agent_notes_service import AgentNotesService

    project_root = tmp_path / "project"
    project_root.mkdir()
    notes = AgentNotesService(
        project_id="demo", project_root=project_root
    )
    executor = _make_executor(tmp_path)
    executor._agent_notes = notes

    result = await executor.execute_tool(
        "append_agent_note",
        {"note": "x" * (AgentNotesService.MAX_NOTE_CHARS + 1)},
    )
    assert result["stored"] is False
    assert result["error"] == "AGENT_NOTE_OVERSIZE"


def test_tl_code_write_tools_are_read_and_gatekeeper_only(tmp_path: Path):
    """When code-write / git / inspector / PR services are all wired,
    TL's surface includes reads + git gatekeeper tools but NOT
    ``write_project_code*`` — those are now developer-only.
    """
    from feishu_agent.tools.code_write_service import CodeWriteService
    from feishu_agent.tools.git_ops_service import GitOpsService
    from feishu_agent.tools.pre_push_inspector import PrePushInspector
    from feishu_agent.tools.pull_request_service import PullRequestService

    executor = _make_executor(tmp_path)
    executor._code_write = MagicMock(spec=CodeWriteService)
    executor._git_ops = MagicMock(spec=GitOpsService)
    executor._pre_push_inspector = MagicMock(spec=PrePushInspector)
    executor._pull_request = MagicMock(spec=PullRequestService)

    names = {s.name for s in executor.tool_specs()}

    # Reads + gatekeeper tools present
    assert "read_project_code" in names
    assert "list_project_paths" in names
    assert "describe_code_write_policy" in names
    assert "run_pre_push_inspection" in names
    assert "git_commit" in names
    assert "git_push" in names
    assert "git_sync_remote" in names
    assert "create_pull_request" in names

    # Code-write tools are stripped — the developer role owns those
    assert "write_project_code" not in names
    assert "write_project_code_batch" not in names


@pytest.mark.asyncio
async def test_tl_write_project_code_refused_at_dispatch(tmp_path: Path):
    """Belt + suspenders: even if the LLM names ``write_project_code``,
    the dispatcher refuses before touching the service."""
    from feishu_agent.tools.code_write_service import CodeWriteService

    executor = _make_executor(tmp_path)
    mock_cw = MagicMock(spec=CodeWriteService)
    executor._code_write = mock_cw

    result = await executor.execute_tool(
        "write_project_code",
        {"relative_path": "a.py", "content": "ok", "reason": "r"},
    )
    assert isinstance(result, dict)
    assert result["error"] == "TOOL_NOT_ALLOWED_ON_ROLE"
    mock_cw.write_source.assert_not_called()


# ======================================================================
# watch_pr_checks — post-PR CI gate
# ======================================================================
#
# These pin the contract that nailed PR #8 to the wall: TL must NOT be
# able to declare success without going through CIWatchService, and the
# tool must only appear in the surface when the service is wired.


def test_watch_pr_checks_absent_when_service_not_wired(tmp_path: Path):
    """Without a CIWatchService, TL must not even SEE ``watch_pr_checks``
    in its tool surface — otherwise the LLM would call a tool that
    doesn't exist and we'd lose the gate-by-construction guarantee."""
    executor = _make_executor(tmp_path)
    executor._ci_watch = None
    names = {s.name for s in executor.tool_specs()}
    assert "watch_pr_checks" not in names


def test_watch_pr_checks_present_when_service_wired(tmp_path: Path):
    from feishu_agent.tools.ci_watch_service import CIWatchService

    executor = _make_executor(tmp_path)
    executor._ci_watch = MagicMock(spec=CIWatchService)
    names = {s.name for s in executor.tool_specs()}
    assert "watch_pr_checks" in names


@pytest.mark.asyncio
async def test_watch_pr_checks_routes_to_ci_watch_service(tmp_path: Path):
    from feishu_agent.tools.ci_watch_service import (
        CIWatchResult,
        CIWatchService,
        FailingJob,
    )

    executor = _make_executor(tmp_path)
    mock_ci = MagicMock(spec=CIWatchService)
    mock_ci.watch.return_value = CIWatchResult(
        status="failure",
        pr_number=42,
        failing_jobs=[
            FailingJob(
                name="miniapp-typecheck",
                workflow="miniapp.yml",
                state="failure",
                link="https://x/y/z",
                description="",
            )
        ],
        summary="PR #42: 1 failing check(s): miniapp-typecheck",
        watched_seconds=3.5,
    )
    executor._ci_watch = mock_ci

    result = await executor.execute_tool(
        "watch_pr_checks",
        {"pr_number": 42, "timeout_seconds": 60, "poll_interval": 5},
    )
    assert isinstance(result, dict)
    assert result["status"] == "failure"
    assert result["pr_number"] == 42
    assert result["failing_jobs"][0]["name"] == "miniapp-typecheck"
    mock_ci.watch.assert_called_once_with(
        project_id=executor.project_id,
        pr_number=42,
        timeout_seconds=60,
        poll_interval=5,
    )


@pytest.mark.asyncio
async def test_watch_pr_checks_surfaces_typed_errors(tmp_path: Path):
    """A raised CIWatchError (genuine config/programming bug — e.g.
    unknown project_id — NOT the routine "gh missing" case which is now
    a graceful ``status=unavailable`` result) must come back as a
    structured tool error so the LLM can branch on ``result["error"]``
    instead of mistaking it for a green CI.

    We specifically use CIWatchProjectError here because it's a genuine
    config error (the executor was wired with a project id that isn't
    in the service's known roots) — exactly the class of failure that
    SHOULD surface as a loud error, not be silently degraded."""
    from feishu_agent.tools.ci_watch_service import (
        CIWatchProjectError,
        CIWatchService,
    )

    executor = _make_executor(tmp_path)
    mock_ci = MagicMock(spec=CIWatchService)
    mock_ci.watch.side_effect = CIWatchProjectError(
        "No project configured for project_id='test-project'."
    )
    executor._ci_watch = mock_ci

    result = await executor.execute_tool(
        "watch_pr_checks", {"pr_number": 1}
    )
    assert isinstance(result, dict)
    assert result["error"] == "CI_WATCH_UNKNOWN_PROJECT"
    assert "project" in result["message"].lower()


@pytest.mark.asyncio
async def test_watch_pr_checks_unavailable_flows_as_result(tmp_path: Path):
    """The "gh missing / unauthenticated" case is NOT a tool error — it
    is a normal result payload with ``status="unavailable"``. The tool
    handler must pass it through unchanged so the LLM's skill-level
    ``if status == 'unavailable':`` branch actually fires.

    This is the contract that PR #8 / the subsequent review found
    violated — the service used to raise ``CIWatchGhMissingError``
    which surfaced as ``{"error": "CI_WATCH_GH_MISSING"}`` with no
    ``status`` field at all, making the skill's documented branch dead
    code. Pin it now so we don't regress."""
    from feishu_agent.tools.ci_watch_service import (
        CIWatchResult,
        CIWatchService,
    )

    executor = _make_executor(tmp_path)
    mock_ci = MagicMock(spec=CIWatchService)
    mock_ci.watch.return_value = CIWatchResult(
        status="unavailable",
        pr_number=1,
        failing_jobs=[],
        summary="PR #1: cannot read CI status — `gh` binary not installed.",
        watched_seconds=0.0,
        reason="gh not found. Install GitHub CLI ...",
    )
    executor._ci_watch = mock_ci

    result = await executor.execute_tool(
        "watch_pr_checks", {"pr_number": 1}
    )
    assert isinstance(result, dict)
    # No error key — must look like a regular success/failure result
    # with ``status`` set.
    assert "error" not in result
    assert result["status"] == "unavailable"
    assert result["reason"] is not None


@pytest.mark.asyncio
async def test_tl_can_call_watch_pr_checks(tmp_path: Path):
    """The TL allow-list must include ``watch_pr_checks`` — otherwise a
    legitimate call would be refused as TOOL_NOT_ALLOWED_ON_ROLE."""
    from feishu_agent.roles.tech_lead_executor import (
        TECH_LEAD_CODE_WRITE_ALLOW,
    )

    assert "watch_pr_checks" in TECH_LEAD_CODE_WRITE_ALLOW


# ======================================================================
# T024 — retained tool tests
# ======================================================================


@pytest.mark.asyncio
async def test_read_sprint_status_returns_data(tmp_path: Path):
    status_data = {
        "sprint_name": "Sprint 3",
        "current_sprint": {
            "goal": "Deliver Phase 2",
            "in_progress": ["3-1-vine-farming-data"],
            "planned": ["3-2-merge-to-tree"],
        },
    }
    executor = _make_executor(tmp_path, status_data=status_data)
    result = await executor.execute_tool("read_sprint_status", {})
    assert result["sprint_name"] == "Sprint 3"
    assert result["goal"] == "Deliver Phase 2"
    assert "in_progress" in result["current_sprint"]


def test_advance_sprint_state_produces_change(tmp_path: Path):
    status_data = {
        "sprint_name": "Sprint 3",
        "current_sprint": {
            "goal": "Test",
            "in_progress": ["3-1-vine-farming-data"],
            "review": [],
            "completed": [],
        },
    }
    executor = _make_executor(tmp_path, status_data=status_data)

    mock_adapter = {"source_roots": {"status_file": "sprint-status.yaml"}}
    executor._progress_sync.load_adapter.return_value = mock_adapter

    mock_record = MagicMock()
    mock_record.status = "in-progress"
    mock_record.story_key = "3-1-vine-farming-data"
    mock_record.native_key = "3-1-vine-farming-data"
    executor._progress_sync.read_records.return_value = [mock_record]
    executor._progress_sync.select_sources.return_value = []
    executor._progress_sync.dedupe_records.return_value = [mock_record]

    args = AdvanceSprintStateArgs(
        story_key="3-1-vine-farming-data",
        to_status="review",
        dry_run=True,
    )
    result = executor._advance_sprint_state(args)
    assert result["dry_run"] is True
    assert len(result["changes"]) > 0
    assert result["changes"][0]["to_status"] == "review"
    assert result["story_key"] == "3-1-vine-farming-data"
    assert result["from_status"] == "in-progress"
    assert result["to_status"] == "review"


@pytest.mark.asyncio
async def test_unsupported_tool_raises(tmp_path: Path):
    executor = _make_executor(tmp_path)
    with pytest.raises(RuntimeError, match="Unsupported tool"):
        await executor.execute_tool("nonexistent_tool", {})


# ======================================================================
# delegate_to_application_agent tests
# ======================================================================


@pytest.mark.asyncio
async def test_delegate_no_group_chat_id(tmp_path: Path):
    executor = _make_executor(tmp_path)
    result = await executor.execute_tool(
        "delegate_to_application_agent", {"message": "读取词汇科学任务管理"}
    )
    assert result["sent"] is False
    err = result["error"]
    assert "delegate_url" in err and "group_chat_id" in err


@pytest.mark.asyncio
async def test_delegate_no_feishu_client(tmp_path: Path):
    executor = _make_executor(tmp_path)
    executor._app_agent_group_chat_id = "oc_test123"
    result = await executor.execute_tool(
        "delegate_to_application_agent", {"message": "读取数据"}
    )
    assert result["sent"] is False
    assert "client" in result["error"].lower()


@pytest.mark.asyncio
async def test_delegate_happy_path(tmp_path: Path):
    executor = _make_executor(tmp_path)
    mock_client = AsyncMock(spec=ManagedFeishuClient)
    mock_client.request.return_value = {"data": {"message_id": "msg_abc123"}}
    executor._feishu_client = mock_client
    executor._app_agent_open_id = "ou_test_bot"
    executor._app_agent_group_chat_id = "oc_group123"
    executor._app_agent_label = "Application delegate"

    result = await executor.execute_tool(
        "delegate_to_application_agent", {"message": "读取词汇科学任务管理"}
    )
    assert result["sent"] is True
    assert result["channel"] == "feishu_im"
    assert result["message_id"] == "msg_abc123"
    assert result["error"] is None

    call_args = mock_client.request.call_args
    body = call_args.kwargs.get("json_body") or call_args[1].get("json_body")
    assert body["receive_id"] == "oc_group123"
    import json
    text_content = json.loads(body["content"])["text"]
    assert text_content == (
        '<at user_id="ou_test_bot">Application delegate</at> 读取词汇科学任务管理'
    )


@pytest.mark.asyncio
async def test_delegate_webhook_happy_path(tmp_path: Path):
    executor = _make_executor(tmp_path)
    executor._app_delegate_url = "https://example.com/delegate/inbox"
    executor._app_agent_label = "Application delegate"
    executor._tech_lead_bot_open_id = "ou_tech_lead_bot"

    class _FakeResp:
        status_code = 204

        def raise_for_status(self) -> None:
            return None

    class _FakeClient:
        captured: dict[str, Any] = {}

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, json: dict[str, Any] | None = None) -> _FakeResp:
            _FakeClient.captured = {"url": url, "json": json or {}}
            return _FakeResp()

    with patch(
        "feishu_agent.roles.tech_lead_executor.httpx.AsyncClient",
        return_value=_FakeClient(),
    ):
        result = await executor.execute_tool(
            "delegate_to_application_agent", {"message": "读取表 A"}
        )

    assert result["sent"] is True
    assert result["channel"] == "delegate_webhook"
    assert result["status_code"] == 204
    assert _FakeClient.captured["url"] == "https://example.com/delegate/inbox"
    body = _FakeClient.captured["json"]
    assert body["source"] == "tech_lead"
    assert body["source_label"] == "技术组长"
    assert body["message"] == "读取表 A"
    assert body["target_agent_label"] == "Application delegate"
    assert body["tech_lead_chat_id"] == "chat-001"
    assert body["tech_lead_bot_open_id"] == "ou_tech_lead_bot"
    assert body["tech_lead_mention_display_name"] == "技术组长"
    assert body["tech_lead_at_text"] == '<at user_id="ou_tech_lead_bot">技术组长</at>'


@pytest.mark.asyncio
async def test_delegate_impersonation_happy_path(tmp_path: Path):
    """When an impersonation service provides a user_access_token and
    the target bot open_id is configured, delegate should POST to Feishu
    as the user (not via the bot ManagedFeishuClient) so OpenClaw-hosted
    bots actually trigger on the @mention."""
    from feishu_agent.runtime.impersonation_token_service import (
        ImpersonationTokenService,
    )

    executor = _make_executor(tmp_path)
    executor._app_agent_open_id = "ou_assistant_bot"
    executor._app_agent_group_chat_id = "oc_group123"
    executor._app_agent_label = "Application delegate"

    imp = MagicMock(spec=ImpersonationTokenService)
    imp.get_access_token = AsyncMock(return_value="u-a-t-abc")
    imp.last_error = None
    executor._impersonation_token_service = imp

    # Also wire the bot client — this test asserts it is NOT called.
    mock_client = AsyncMock(spec=ManagedFeishuClient)
    executor._feishu_client = mock_client

    class _Resp:
        status_code = 200
        content = b'{"code":0,"data":{"message_id":"om_xyz"}}'

        def json(self) -> dict[str, Any]:
            return {"code": 0, "data": {"message_id": "om_xyz"}}

    class _FakeClient:
        captured: dict[str, Any] = {}

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(
            self,
            url: str,
            params: dict[str, Any] | None = None,
            headers: dict[str, Any] | None = None,
            json: dict[str, Any] | None = None,
        ) -> _Resp:
            _FakeClient.captured = {
                "url": url,
                "params": params or {},
                "headers": headers or {},
                "json": json or {},
            }
            return _Resp()

    with patch(
        "feishu_agent.roles.tech_lead_executor.httpx.AsyncClient",
        return_value=_FakeClient(),
    ):
        result = await executor.execute_tool(
            "delegate_to_application_agent", {"message": "读取 sprint-status"}
        )

    assert result["sent"] is True
    assert result["channel"] == "feishu_im_as_user"
    assert result["message_id"] == "om_xyz"
    assert result["error"] is None

    captured = _FakeClient.captured
    assert captured["url"] == "https://open.feishu.cn/open-apis/im/v1/messages"
    assert captured["params"] == {"receive_id_type": "chat_id"}
    assert captured["headers"]["Authorization"] == "Bearer u-a-t-abc"
    body = captured["json"]
    assert body["receive_id"] == "oc_group123"
    import json as _json
    text = _json.loads(body["content"])["text"]
    assert text == '<at user_id="ou_assistant_bot">Application delegate</at> 读取 sprint-status'
    # Bot-IM client must not be used when impersonation succeeds.
    mock_client.request.assert_not_called()


@pytest.mark.asyncio
async def test_delegate_impersonation_missing_token_falls_back(tmp_path: Path):
    """When impersonation service is wired but no token is available
    (e.g. operator never ran the auth-server probe), delegate should
    still deliver the message via bot-IM and surface a warning so the
    operator notices impersonation is degraded."""
    from feishu_agent.runtime.impersonation_token_service import (
        ImpersonationTokenService,
    )

    executor = _make_executor(tmp_path)
    executor._app_agent_open_id = "ou_assistant_bot"
    executor._app_agent_group_chat_id = "oc_group123"
    executor._app_agent_label = "Application delegate"

    imp = MagicMock(spec=ImpersonationTokenService)
    imp.get_access_token = AsyncMock(return_value=None)
    imp.last_error = "no user token at /tmp/...; run spikes/probe_as_user.py auth-server"
    executor._impersonation_token_service = imp

    mock_client = AsyncMock(spec=ManagedFeishuClient)
    mock_client.request.return_value = {"data": {"message_id": "msg_bot"}}
    executor._feishu_client = mock_client

    result = await executor.execute_tool(
        "delegate_to_application_agent", {"message": "hi"}
    )
    assert result["sent"] is True
    assert result["channel"] == "feishu_im"
    assert result["message_id"] == "msg_bot"
    assert "auth-server" in result.get("impersonation_warning", "")
    mock_client.request.assert_called_once()


@pytest.mark.asyncio
async def test_delegate_api_exception(tmp_path: Path):
    executor = _make_executor(tmp_path)
    mock_client = AsyncMock(spec=ManagedFeishuClient)
    mock_client.request.side_effect = RuntimeError("network timeout")
    executor._feishu_client = mock_client
    executor._app_agent_group_chat_id = "oc_group123"

    result = await executor.execute_tool(
        "delegate_to_application_agent", {"message": "更新状态"}
    )
    assert result["sent"] is False
    assert result.get("channel") == "feishu_im"
    assert "network timeout" in result["error"]


# ======================================================================
# _remaining_seconds budget tests
# ======================================================================


def test_remaining_seconds_normal(tmp_path: Path):
    executor = _make_executor(tmp_path)
    executor.timeout_seconds = 180
    remaining = executor._remaining_seconds(reserve=30)
    assert 100 < remaining <= 150


def test_remaining_seconds_floor_at_15(tmp_path: Path):
    executor = _make_executor(tmp_path)
    executor.timeout_seconds = 10
    remaining = executor._remaining_seconds(reserve=30)
    assert remaining == 15


# ======================================================================
# thread_update_fn callback tests
# ======================================================================


def _make_executor_with_callback(
    tmp_path: Path,
    callback: Any = None,
) -> TechLeadToolExecutor:
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir(exist_ok=True)
    audit_dir = tmp_path / "audit"
    status_file = "sprint-status.yaml"

    mock_sync = MagicMock(spec=ProgressSyncService)
    mock_sync.repo_root = tmp_path
    mock_llm = MagicMock(spec=LlmAgentAdapter)

    return TechLeadToolExecutor(
        progress_sync_service=mock_sync,
        sprint_state_service=SprintStateService(tmp_path, status_file),
        audit_service=AuditService(audit_dir),
        llm_agent_adapter=mock_llm,
        role_registry=RoleRegistryService(roles_dir),
        project_id="test-project",
        command_text="test command",
        trace_id="trace-001",
        chat_id="chat-001",
        thread_update_fn=callback,
        # Explicit large budget so ``_dispatch_role_agent`` clears the
        # ``MIN_SUB_AGENT_TIMEOUT_SECONDS`` floor in the callback tests
        # below. Without this, those tests would hit the new
        # OUT_OF_BUDGET short-circuit instead of exercising the
        # callback emission paths they are actually pinning.
        timeout_seconds=600,
    )


def test_fire_thread_update_noop_when_none(tmp_path: Path):
    executor = _make_executor_with_callback(tmp_path, callback=None)
    executor._fire_thread_update("test message")


def test_fire_thread_update_calls_callback(tmp_path: Path):
    callback = MagicMock()
    executor = _make_executor_with_callback(tmp_path, callback=callback)
    executor._fire_thread_update("hello")
    callback.assert_called_once_with("hello")


def test_fire_thread_update_swallows_exception(tmp_path: Path):
    callback = MagicMock(side_effect=RuntimeError("network error"))
    executor = _make_executor_with_callback(tmp_path, callback=callback)
    executor._fire_thread_update("should not raise")
    callback.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_fires_callback_before_and_after(tmp_path: Path):
    calls: list[str] = []
    callback = MagicMock(side_effect=lambda text: calls.append(text))

    executor = _make_executor_with_callback(tmp_path, callback=callback)

    role_md = tmp_path / "roles" / "test_role.md"
    role_md.write_text("---\nrole_name: test_role\nsystem_prompt: You are a test role.\n---\n")
    executor._role_registry = RoleRegistryService(tmp_path / "roles")

    mock_result = MagicMock()
    mock_result.success = True
    mock_result.content = "done"
    mock_result.error_message = None
    mock_result.latency_ms = 500
    executor._llm_agent.spawn_sub_agent = AsyncMock(return_value=mock_result)

    result = await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "test_role", "task": "do something"},
    )

    assert result["success"] is True
    assert len(calls) == 2
    assert "已委派" in calls[0]
    assert "test_role" in calls[0]
    assert "✅" in calls[1]


@pytest.mark.asyncio
async def test_dispatch_fires_failure_callback_on_timeout(tmp_path: Path):
    calls: list[str] = []
    callback = MagicMock(side_effect=lambda text: calls.append(text))

    executor = _make_executor_with_callback(tmp_path, callback=callback)

    role_md = tmp_path / "roles" / "timeout_role.md"
    role_md.write_text("---\nrole_name: timeout_role\nsystem_prompt: You are a role.\n---\n")
    executor._role_registry = RoleRegistryService(tmp_path / "roles")
    executor._llm_agent.spawn_sub_agent = AsyncMock(side_effect=TimeoutError("timed out"))

    result = await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "timeout_role", "task": "slow task"},
    )

    assert result["success"] is False
    assert result["error"] == "AGENT_TIMEOUT"
    assert len(calls) == 2
    assert "已委派" in calls[0]
    assert "❌" in calls[1]
    assert "超时" in calls[1]


@pytest.mark.asyncio
async def test_dispatch_failure_shows_error_reason(tmp_path: Path):
    calls: list[str] = []
    callback = MagicMock(side_effect=lambda text: calls.append(text))

    executor = _make_executor_with_callback(tmp_path, callback=callback)

    role_md = tmp_path / "roles" / "fail_role.md"
    role_md.write_text("---\nrole_name: fail_role\nsystem_prompt: You are a role.\n---\n")
    executor._role_registry = RoleRegistryService(tmp_path / "roles")

    mock_result = MagicMock()
    mock_result.success = False
    mock_result.content = ""
    mock_result.error_message = "LLM returned empty content"
    mock_result.latency_ms = 2000
    executor._llm_agent.spawn_sub_agent = AsyncMock(return_value=mock_result)

    result = await executor.execute_tool(
        "dispatch_role_agent",
        {"role_name": "fail_role", "task": "broken task"},
    )

    assert result["success"] is False
    assert len(calls) == 2
    assert "❌" in calls[1]
    assert "原因" in calls[1]
    assert "LLM returned empty content" in calls[1]


def test_truncate_task_short():
    assert TechLeadToolExecutor._truncate_task("short task") == "short task"


def test_truncate_task_long():
    long_task = "a" * 100
    result = TechLeadToolExecutor._truncate_task(long_task)
    assert len(result) == 61
    assert result.endswith("…")


def test_truncate_task_exact_limit():
    exact = "a" * 60
    assert TechLeadToolExecutor._truncate_task(exact) == exact


# ======================================================================
# run_speckit_script surface (TL has a DIFFERENT whitelist than PM)
# ======================================================================


from feishu_agent.tools.speckit_script_service import (  # noqa: E402
    ScriptNotAllowedError,
    SpeckitScriptError,
    SpeckitScriptResult,
    SpeckitScriptService,
)


class _StubSpeckitService(SpeckitScriptService):
    """Records calls + returns canned json; NEVER invokes bash."""

    def __init__(self, *, raise_with: SpeckitScriptError | None = None):
        super().__init__(project_roots={"test-project": Path("/tmp/tl-project")})
        self.calls: list[dict] = []
        self._raise = raise_with

    def run_script(self, *, agent_name, project_id, script, args=None):
        self.calls.append(
            {
                "agent_name": agent_name,
                "project_id": project_id,
                "script": script,
                "args": list(args or []),
            }
        )
        if self._raise is not None:
            raise self._raise
        return SpeckitScriptResult(
            script=script,
            argv=tuple(args or []),
            exit_code=0,
            success=True,
            stdout='{"BRANCH":"004-foo","IMPL_PLAN":"specs/004-foo/plan.md"}\n',
            stderr="",
            parsed_json={
                "BRANCH": "004-foo",
                "IMPL_PLAN": "specs/004-foo/plan.md",
            },
            elapsed_ms=33,
        )


def _make_tl_executor_with_speckit(
    tmp_path: Path,
    *,
    speckit_service: SpeckitScriptService | None,
) -> TechLeadToolExecutor:
    roles_dir = tmp_path / "roles"
    roles_dir.mkdir()
    audit_dir = tmp_path / "audit"
    status_file = "sprint-status.yaml"
    mock_sync = MagicMock(spec=ProgressSyncService)
    mock_sync.repo_root = tmp_path
    mock_llm = MagicMock(spec=LlmAgentAdapter)
    return TechLeadToolExecutor(
        progress_sync_service=mock_sync,
        sprint_state_service=SprintStateService(tmp_path, status_file),
        audit_service=AuditService(audit_dir),
        llm_agent_adapter=mock_llm,
        role_registry=RoleRegistryService(roles_dir),
        speckit_script_service=speckit_service,
        project_id="test-project",
        command_text="",
        trace_id="trace-tl",
        chat_id="chat-tl",
    )


def test_tl_speckit_tool_absent_when_service_not_wired(tmp_path: Path):
    executor = _make_tl_executor_with_speckit(tmp_path, speckit_service=None)
    names = {s.name for s in executor.tool_specs()}
    assert "run_speckit_script" not in names


def test_tl_speckit_tool_present_when_service_wired(tmp_path: Path):
    executor = _make_tl_executor_with_speckit(
        tmp_path, speckit_service=_StubSpeckitService()
    )
    names = {s.name for s in executor.tool_specs()}
    assert "run_speckit_script" in names


@pytest.mark.asyncio
async def test_tl_speckit_dispatches_with_tech_lead_agent_name(tmp_path: Path):
    stub = _StubSpeckitService()
    executor = _make_tl_executor_with_speckit(tmp_path, speckit_service=stub)
    result = await executor.execute_tool(
        "run_speckit_script",
        {"script": "setup-plan.sh", "args": ["--json"]},
    )
    assert result["success"] is True
    assert result["parsed_json"]["BRANCH"] == "004-foo"
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["agent_name"] == "tech_lead"
    assert call["project_id"] == "test-project"
    assert call["script"] == "setup-plan.sh"
    assert call["args"] == ["--json"]


@pytest.mark.asyncio
async def test_tl_speckit_script_not_allowed_error_is_surfaced(tmp_path: Path):
    """If TL asks for ``create-new-feature.sh`` the service rejects at
    the ACL layer; the executor must translate that to a typed payload,
    not raise."""

    stub = _StubSpeckitService(
        raise_with=ScriptNotAllowedError(
            "create-new-feature.sh is not allowed for tech_lead"
        ),
    )
    executor = _make_tl_executor_with_speckit(tmp_path, speckit_service=stub)
    result = await executor.execute_tool(
        "run_speckit_script",
        {"script": "create-new-feature.sh", "args": ["--json", "Foo"]},
    )
    assert result["error"] == "SCRIPT_NOT_ALLOWED_FOR_AGENT"
    assert "tech_lead" in result["message"]


@pytest.mark.asyncio
async def test_tl_publish_artifacts_not_advertised(tmp_path: Path):
    """TL must NOT see ``publish_artifacts`` — the service is
    PM-scoped by design (doc-only commit+push surface). Regression
    guard: if someone adds TL to ``_AGENT_ALLOWED_PUBLISH_ROOTS``
    this assertion fires."""

    executor = _make_tl_executor_with_speckit(tmp_path, speckit_service=None)
    names = {s.name for s in executor.tool_specs()}
    assert "publish_artifacts" not in names
