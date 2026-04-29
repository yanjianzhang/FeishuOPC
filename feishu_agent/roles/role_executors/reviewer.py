from __future__ import annotations

from typing import Any, Callable

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.roles.role_executors.tool_handlers import (
    read_bitable_rows,
    read_bitable_schema,
    read_sprint_status,
)
from feishu_agent.team.role_artifact_writer import RoleArtifactWriter
from feishu_agent.team.sprint_state_service import SprintStateService
from feishu_agent.tools.code_write_service import CodeWriteService
from feishu_agent.tools.code_write_tools import CodeWriteToolsMixin
from feishu_agent.tools.feishu_agent_tools import (
    ReadBitableRowsArgs,
    ReadBitableSchemaArgs,
    SprintArgs,
    _tool_spec,
)
from feishu_agent.tools.progress_sync_service import ProgressSyncService
from feishu_agent.tools.workflow_service import WorkflowService
from feishu_agent.tools.workflow_tools import WorkflowToolsMixin

# Reviewer's code surface is READ-ONLY. The mixin offers a lot more
# than we want here, so the allow-filter narrows the surface to the
# three inspection tools; writes, commits, pushes, and pre-push
# inspection all stay refused by the filter (both at tool_specs time
# and at dispatch time).
REVIEWER_CODE_READ_ALLOW: frozenset[str] = frozenset(
    {
        "describe_code_write_policy",
        "read_project_code",
        "list_project_paths",
    }
)


REVIEWER_TOOL_SPECS = [
    _tool_spec(
        "read_sprint_status",
        "Read the current sprint status file and return the goal plus current sprint lists.",
        SprintArgs,
    ),
    _tool_spec(
        "read_bitable_rows",
        "Read existing Feishu Bitable rows from the selected table, optionally filtering by a free-text search string across returned fields.",
        ReadBitableRowsArgs,
    ),
    _tool_spec(
        "read_bitable_schema",
        "Read the live Feishu Bitable field schema for the selected table so you can understand available columns before reading or writing rows.",
        ReadBitableSchemaArgs,
    ),
]


class ReviewerExecutor(CodeWriteToolsMixin, WorkflowToolsMixin):
    """AgentToolExecutor for the reviewer role.

    Static tools: read_sprint_status, read_bitable_rows,
    read_bitable_schema.

    When a ``CodeWriteService`` is wired in, the executor also
    surfaces three READ-ONLY code tools
    (``describe_code_write_policy`` / ``read_project_code`` /
    ``list_project_paths``) so the reviewer can read the code it is
    reviewing. Writes / commits / inspection stay blocked by the
    allow-filter at both spec time AND dispatch time, so even stale
    LLM context cannot punch through.

    When a ``RoleArtifactWriter`` is wired in, the reviewer also
    gets ``write_role_artifact`` to persist its findings under
    ``docs/reviews/<story-id>-review.md`` — that artifact is how
    tech lead receives review output (TL reads the file, never
    re-runs the review from chat).
    """

    def __init__(
        self,
        *,
        sprint_state_service: SprintStateService,
        progress_sync_service: ProgressSyncService,
        project_id: str = "",
        command_text: str = "",
        role_name: str = "reviewer",
        load_bitable_tables: Callable[[], dict[str, Any]] = lambda: {},
        load_role_permissions: Callable[[str], list[Any]] = lambda _: [],
        build_progress_sync_service_for_target: Callable[[str | None, Any | None], ProgressSyncService] | None = None,
        role_artifact_writer: RoleArtifactWriter | None = None,
        code_write_service: CodeWriteService | None = None,
        workflow_service: WorkflowService | None = None,
        **_kwargs: Any,
    ) -> None:
        self._sprint_state = sprint_state_service
        self._progress_sync = progress_sync_service
        self.project_id = project_id
        self.command_text = command_text
        self.role_name = role_name
        self.load_bitable_tables = load_bitable_tables
        self.load_role_permissions = load_role_permissions
        self._build_progress_sync_service_for_target = build_progress_sync_service_for_target
        self._role_artifact_writer = role_artifact_writer

        # Wire CodeWriteToolsMixin. Only ``_code_write`` is ever set —
        # the other service slots stay None so the mixin never
        # advertises git / inspector / PR tools. The allow-filter is
        # the second belt that keeps the surface read-only even if a
        # future refactor accidentally wires a writable service in.
        self._code_write = code_write_service
        self._pre_push_inspector = None
        self._git_ops = None
        self._pull_request = None
        self._ci_watch = None
        self._code_write_tool_allow = REVIEWER_CODE_READ_ALLOW

        # Read-only workflow-tool surface. When WorkflowService is wired
        # in, reviewer can load the bmad:code-review methodology file
        # with ``read_workflow_instruction("bmad:code-review")`` and
        # browse prior review artifacts with ``list_workflow_artifacts``.
        # The readonly flag strips the two write tools from both the
        # spec list and the dispatch path, so artifact creation stays
        # with tech_lead / prd_writer.
        self._workflow = workflow_service
        self._workflow_agent_name = role_name
        self._workflow_readonly = True

    def tool_specs(self) -> list[AgentToolSpec]:
        specs = list(REVIEWER_TOOL_SPECS)
        specs.extend(self.code_write_tool_specs())
        specs.extend(self.workflow_tool_specs())
        if self._role_artifact_writer is not None:
            specs.extend(self._role_artifact_writer.tool_specs())
        return specs

    def _emit_code_write_update(self, line: str) -> None:  # no-op for reviewer
        return None

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | list[Any] | str:
        if self._role_artifact_writer is not None:
            handled = self._role_artifact_writer.try_handle(tool_name, arguments)
            if handled is not None:
                return handled
        if tool_name == "read_sprint_status":
            parsed = SprintArgs.model_validate(arguments)
            return read_sprint_status(self._sprint_state, parsed.sprint)
        if tool_name == "read_bitable_rows":
            parsed = ReadBitableRowsArgs.model_validate(arguments)
            return await read_bitable_rows(
                table_name=parsed.table_name,
                search_text=parsed.search_text,
                field_names=parsed.field_names,
                page_token=parsed.page_token,
                limit=parsed.limit,
                role_name=self.role_name,
                load_bitable_tables=self.load_bitable_tables,
                load_role_permissions=self.load_role_permissions,
                sync_service_builder=self._build_progress_sync_service_for_target,
                fallback_sync_service=self._progress_sync,
            )
        if tool_name == "read_bitable_schema":
            parsed = ReadBitableSchemaArgs.model_validate(arguments)
            return await read_bitable_schema(
                table_name=parsed.table_name,
                role_name=self.role_name,
                load_bitable_tables=self.load_bitable_tables,
                load_role_permissions=self.load_role_permissions,
                sync_service_builder=self._build_progress_sync_service_for_target,
                fallback_sync_service=self._progress_sync,
            )
        code_result = await self.handle_code_write_tool(tool_name, arguments)
        if code_result is not None:
            return code_result
        workflow_result = await self.handle_workflow_tool(tool_name, arguments)
        if workflow_result is not None:
            return workflow_result
        raise RuntimeError(f"Unsupported tool: {tool_name}")
