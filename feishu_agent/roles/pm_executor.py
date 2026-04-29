from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from feishu_agent.core.agent_types import (
    AgentToolExecutor,
    AgentToolSpec,
    AllowListedToolExecutor,
)
from feishu_agent.core.llm_agent_adapter import LlmAgentAdapter
from feishu_agent.roles.role_registry_service import (
    RoleDefinition,
    RoleNotFoundError,
    RoleRegistryService,
)
from feishu_agent.runtime.managed_feishu_client import ManagedFeishuClient
from feishu_agent.team.artifact_publish_service import ArtifactPublishService
from feishu_agent.tools.feishu_agent_tools import _tool_spec
from feishu_agent.tools.speckit_script_service import SpeckitScriptService
from feishu_agent.tools.workflow_service import WorkflowService
from feishu_agent.tools.workflow_tools import (
    ArtifactPublishMixin,
    SpeckitScriptMixin,
    WorkflowToolsMixin,
)

logger = logging.getLogger(__name__)

RoleExecutorProvider = Callable[[str, RoleDefinition], AgentToolExecutor | None]


class DispatchRoleAgentArgs(BaseModel):
    role_name: str = Field(description="Role agent to dispatch (e.g. 'prd_writer', 'researcher')")
    task: str = Field(description="Natural language task description for the role agent")
    acceptance_criteria: str = Field(default="", description="Expected output format or acceptance criteria")


class NotifyTechLeadArgs(BaseModel):
    message: str = Field(description="Message to send to TechLead (Chinese, concise summary of the PRD or status)")


PM_TOOL_SPECS = [
    _tool_spec(
        "dispatch_role_agent",
        "Dispatch a role agent to perform a specific task. Use this to delegate PRD writing to prd_writer or research tasks to researcher.",
        DispatchRoleAgentArgs,
    ),
    _tool_spec(
        "notify_tech_lead",
        "Send a notification message to TechLead's Feishu chat. Use this after a PRD is generated to inform TechLead for review and scheduling.",
        NotifyTechLeadArgs,
    ),
]


class PMToolExecutor(WorkflowToolsMixin, SpeckitScriptMixin, ArtifactPublishMixin):
    """AgentToolExecutor for the Product Manager bot.

    Tools: dispatch_role_agent, notify_tech_lead, the workflow tools
    from WorkflowToolsMixin (when a WorkflowService is wired),
    run_speckit_script from SpeckitScriptMixin (when a
    SpeckitScriptService is wired), and publish_artifacts from
    ArtifactPublishMixin (when an ArtifactPublishService is wired).
    The latter two let PM execute the narrow subset of bash scripts
    ``speckit.specify`` requires AND commit+push the resulting spec
    files — neither by granting a generic ``run_shell`` or
    ``git_commit`` tool, but through per-path / per-script
    whitelists enforced server-side.
    """

    def __init__(
        self,
        *,
        llm_agent_adapter: LlmAgentAdapter,
        role_registry: RoleRegistryService,
        feishu_client: ManagedFeishuClient | None = None,
        notify_chat_id: str | None = None,
        timeout_seconds: int = 120,
        role_executor_provider: RoleExecutorProvider | None = None,
        workflow_service: WorkflowService | None = None,
        speckit_script_service: SpeckitScriptService | None = None,
        artifact_publish_service: ArtifactPublishService | None = None,
        project_id: str = "",
        **_kwargs: Any,
    ) -> None:
        self._llm_agent = llm_agent_adapter
        self._role_registry = role_registry
        self._feishu_client = feishu_client
        self._notify_chat_id = notify_chat_id
        self.timeout_seconds = timeout_seconds
        self._role_executor_provider = role_executor_provider
        self._workflow = workflow_service
        self._speckit_scripts = speckit_script_service
        self._artifact_publish = artifact_publish_service
        self._workflow_agent_name = "product_manager"
        self.project_id = project_id

    def tool_specs(self) -> list[AgentToolSpec]:
        return (
            list(PM_TOOL_SPECS)
            + self.workflow_tool_specs()
            + self.speckit_script_tool_specs()
            + self.artifact_publish_tool_specs()
        )

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | list[Any] | str:
        if tool_name == "dispatch_role_agent":
            parsed = DispatchRoleAgentArgs.model_validate(arguments)
            return await self._dispatch_role_agent(parsed)
        if tool_name == "notify_tech_lead":
            parsed = NotifyTechLeadArgs.model_validate(arguments)
            return await self._notify_tech_lead(parsed)
        speckit_result = await self.handle_speckit_script_tool(tool_name, arguments)
        if speckit_result is not None:
            return speckit_result
        publish_result = await self.handle_artifact_publish_tool(tool_name, arguments)
        if publish_result is not None:
            return publish_result
        workflow_result = await self.handle_workflow_tool(tool_name, arguments)
        if workflow_result is not None:
            return workflow_result
        raise RuntimeError(f"Unsupported tool: {tool_name}")

    async def _dispatch_role_agent(self, args: DispatchRoleAgentArgs) -> dict[str, Any]:
        start = time.monotonic()
        try:
            role = self._role_registry.get_role(args.role_name)
        except RoleNotFoundError:
            return {
                "role_name": args.role_name,
                "task": args.task,
                "success": False,
                "output": "",
                "error": f"UNKNOWN_ROLE: {args.role_name}",
                "latency_ms": int((time.monotonic() - start) * 1000),
            }

        prompt = role.system_prompt
        if args.acceptance_criteria:
            prompt += f"\n\nAcceptance criteria: {args.acceptance_criteria}"

        sub_executor: AgentToolExecutor | None = None
        if self._role_executor_provider is not None:
            try:
                sub_executor = self._role_executor_provider(args.role_name, role)
            except Exception:
                logger.exception("role_executor_provider failed for %s", args.role_name)
                sub_executor = None

        try:
            if sub_executor is not None:
                wrapped = AllowListedToolExecutor(
                    sub_executor, role.tool_allow_list or None
                )
                result = await self._llm_agent.spawn_sub_agent_with_tools(
                    role_name=args.role_name,
                    task=args.task,
                    system_prompt=prompt,
                    tool_executor=wrapped,
                    model=role.model,
                    timeout=self.timeout_seconds,
                )
            else:
                result = await self._llm_agent.spawn_sub_agent(
                    role_name=args.role_name,
                    task=args.task,
                    system_prompt=prompt,
                    tools_allow=role.tool_allow_list or None,
                    model=role.model,
                    timeout=self.timeout_seconds,
                )
        except TimeoutError:
            return {
                "role_name": args.role_name,
                "task": args.task,
                "success": False,
                "output": "",
                "error": "AGENT_TIMEOUT",
                "latency_ms": int((time.monotonic() - start) * 1000),
            }
        except Exception as exc:
            return {
                "role_name": args.role_name,
                "task": args.task,
                "success": False,
                "output": "",
                "error": str(exc),
                "latency_ms": int((time.monotonic() - start) * 1000),
            }

        return {
            "role_name": args.role_name,
            "task": args.task,
            "success": result.success,
            "output": result.content,
            "error": result.error_message,
            "latency_ms": result.latency_ms or int((time.monotonic() - start) * 1000),
        }

    async def _notify_tech_lead(self, args: NotifyTechLeadArgs) -> dict[str, Any]:
        if not self._notify_chat_id:
            return {
                "sent": False,
                "chat_id": None,
                "message_id": None,
                "error": "notify_chat_id not configured. Set PM_NOTIFY_TECH_LEAD_CHAT_ID.",
            }
        if not self._feishu_client:
            return {
                "sent": False,
                "chat_id": self._notify_chat_id,
                "message_id": None,
                "error": "Feishu client not available.",
            }

        try:
            payload = await self._feishu_client.request(
                "POST",
                "/open-apis/im/v1/messages?receive_id_type=chat_id",
                json_body={
                    "receive_id": self._notify_chat_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": args.message}),
                },
            )
            message_id = payload.get("message_id") or payload.get("data", {}).get("message_id")
            return {
                "sent": True,
                "chat_id": self._notify_chat_id,
                "message_id": message_id,
                "error": None,
            }
        except Exception as exc:
            return {
                "sent": False,
                "chat_id": self._notify_chat_id,
                "message_id": None,
                "error": str(exc),
            }
