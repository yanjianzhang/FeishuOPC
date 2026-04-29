"""Developer role executor.

Purpose
-------
Tech lead used to own everything that touched source code (write +
inspect + commit + push + PR). That made TL a bottleneck: the same
agent had to reason about sprint state, dispatch sub-agents, review
other agents' output, AND actually type the code — all in one LLM
context. Trust-wise it also merged "author" and "gatekeeper", which
is the classic anti-pattern.

This executor splits those responsibilities:

- **developer** (this class): write code on the branch the tech lead
  already cut for them, make logical commits, leave an implementation
  note as a role artifact. Cannot push, cannot create a PR, cannot
  run pre-push inspection, **cannot sync / fetch / pull / switch
  branches** — branch lifecycle belongs to the tech lead.
- **tech_lead** (unchanged gatekeeper set): start_work_branch,
  git_sync_remote, run_pre_push_inspection, git_commit (for
  last-mile fixups), git_push, create_pull_request. No longer has
  ``write_project_code*``.

Both end up working against the SAME ``CodeWriteService`` and
``GitOpsService`` instances wired from the runtime. The tool
surface — not service wiring — is how we enforce the split. The
mixin's ``_code_write_tool_allow`` filter applies belt + suspenders:
filtered names are removed from ``tool_specs`` AND refused at
dispatch time, so even stale LLM context cannot punch through.
"""

from __future__ import annotations

from typing import Any, Callable

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.roles.role_executors.tool_handlers import (
    read_sprint_status,
)
from feishu_agent.team.role_artifact_writer import RoleArtifactWriter
from feishu_agent.team.sprint_state_service import SprintStateService
from feishu_agent.tools.code_write_service import CodeWriteService
from feishu_agent.tools.code_write_tools import CodeWriteToolsMixin
from feishu_agent.tools.feishu_agent_tools import SprintArgs, _tool_spec
from feishu_agent.tools.git_ops_service import GitOpsService
from feishu_agent.tools.workflow_service import WorkflowService
from feishu_agent.tools.workflow_tools import WorkflowToolsMixin

# The developer's allow-set is intentionally tight. Additions here are
# a trust-boundary change — keep this list under code review.
DEVELOPER_CODE_WRITE_ALLOW: frozenset[str] = frozenset(
    {
        "describe_code_write_policy",
        "read_project_code",
        "list_project_paths",
        "write_project_code",
        "write_project_code_batch",
        # Intentionally NO git_sync_remote / start_work_branch /
        # git_push / git_fetch — branch lifecycle is tech-lead-only.
        # The tech lead cuts a fresh branch from origin/main via
        # start_work_branch before dispatching us, so there is
        # nothing for the developer to sync.
        "git_commit",
    }
)


DEVELOPER_LOCAL_TOOL_SPECS: list[AgentToolSpec] = [
    _tool_spec(
        "read_sprint_status",
        "Read the current sprint status file to remind yourself what "
        "story / task you are implementing. Call this first in each "
        "session if the tech lead didn't quote the story id already.",
        SprintArgs,
    ),
]


class DeveloperExecutor(CodeWriteToolsMixin, WorkflowToolsMixin):
    """AgentToolExecutor for the developer role.

    Tool surface (when fully wired):

    - ``read_sprint_status`` — remind itself of the task
    - ``describe_code_write_policy`` — learn the allowed_write_roots
    - ``read_project_code`` / ``list_project_paths`` — explore the repo
    - ``write_project_code`` / ``write_project_code_batch`` — the main job
    - ``git_commit`` — checkpoint logical units of work
    - ``write_role_artifact`` — drop an implementation note for the TL

    Explicitly NOT in this surface: ``start_work_branch``,
    ``git_sync_remote``, ``git_fetch``, ``git_pull``, ``git_push``,
    ``run_pre_push_inspection``, ``create_pull_request``. The tech
    lead has already cut the branch from origin/main before
    dispatching us, so the developer never has a reason to touch
    remote / branch state. All of that belongs to the tech lead,
    who is the sole gatekeeper. The mixin filter guarantees
    this even if a future wiring bug passes the underlying service
    instance in.
    """

    def __init__(
        self,
        *,
        sprint_state_service: SprintStateService,
        code_write_service: CodeWriteService | None = None,
        git_ops_service: GitOpsService | None = None,
        role_artifact_writer: RoleArtifactWriter | None = None,
        workflow_service: WorkflowService | None = None,
        project_id: str = "",
        command_text: str = "",
        role_name: str = "developer",
        thread_update_fn: Callable[[str], Any] | None = None,
        **_kwargs: Any,
    ) -> None:
        self._sprint_state = sprint_state_service
        self._code_write = code_write_service
        # Developer can commit and sync, but cannot push — the filter
        # strips push/PR even though we hand it the full GitOpsService.
        self._git_ops = git_ops_service
        self._pre_push_inspector = None  # TL-only gatekeeper
        self._pull_request = None  # TL-only gatekeeper
        self._ci_watch = None  # TL-only post-PR gate
        self._role_artifact_writer = role_artifact_writer
        self._thread_update = thread_update_fn

        self._code_write_tool_allow = DEVELOPER_CODE_WRITE_ALLOW

        # Read-only workflow surface. Lets developer load bmad:dev-story
        # methodology (and bug_fixer load bmad:correct-course) before
        # writing code. Cannot produce workflow artifacts — those stay
        # with tech_lead. ``bmad:dev-story`` gives explicit checkpoints
        # (plan → implement → test → update story) that keep the
        # session from drifting into huge unfocused batches.
        self._workflow = workflow_service
        self._workflow_agent_name = role_name
        self._workflow_readonly = True

        self.project_id = project_id
        self.command_text = command_text
        self.role_name = role_name

    # --- mixin required hook ------------------------------------------

    def _emit_code_write_update(self, line: str) -> None:
        if self._thread_update is None:
            return
        try:
            self._thread_update(line)
        except Exception:  # pragma: no cover — best-effort
            pass

    # --- executor interface -------------------------------------------

    def tool_specs(self) -> list[AgentToolSpec]:
        specs: list[AgentToolSpec] = list(DEVELOPER_LOCAL_TOOL_SPECS)
        specs.extend(self.code_write_tool_specs())
        specs.extend(self.workflow_tool_specs())
        if self._role_artifact_writer is not None:
            specs.extend(self._role_artifact_writer.tool_specs())
        return specs

    async def execute_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | list[Any] | str:
        if self._role_artifact_writer is not None:
            handled = self._role_artifact_writer.try_handle(
                tool_name, arguments
            )
            if handled is not None:
                return handled

        if tool_name == "read_sprint_status":
            parsed = SprintArgs.model_validate(arguments)
            return read_sprint_status(self._sprint_state, parsed.sprint)

        code_result = await self.handle_code_write_tool(tool_name, arguments)
        if code_result is not None:
            return code_result

        workflow_result = await self.handle_workflow_tool(tool_name, arguments)
        if workflow_result is not None:
            return workflow_result

        raise RuntimeError(f"Unsupported tool: {tool_name}")
