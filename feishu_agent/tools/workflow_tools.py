"""Shared workflow-command tool plumbing for PM and TechLead executors.

Defines the four workflow tools and a mixin that wires them into any
executor that exposes ``self._workflow`` (``WorkflowService | None``),
``self._workflow_agent_name`` (``str``), and ``self.project_id`` (``str``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.team.artifact_publish_service import (
    ArtifactPublishError,
    ArtifactPublishService,
)
from feishu_agent.tools.feishu_agent_tools import _tool_spec
from feishu_agent.tools.speckit_script_service import (
    SpeckitScriptError,
    SpeckitScriptService,
)
from feishu_agent.tools.workflow_service import WorkflowError, WorkflowService

# ---------------------------------------------------------------------------
# Arg models
# ---------------------------------------------------------------------------


class ReadWorkflowInstructionArgs(BaseModel):
    workflow_id: str = Field(
        description="Workflow command id, e.g. 'speckit.plan', 'bmad:create-story'."
    )


class WriteWorkflowArtifactArgs(BaseModel):
    workflow_id: str = Field(description="Workflow command id this artifact belongs to.")
    relative_path: str = Field(
        description="Path relative to the workflow's artifact root "
        "(e.g. '003-feature/plan.md' for speckit.plan)."
    )
    content: str = Field(description="UTF-8 text content to write.")


class _WorkflowArtifactFile(BaseModel):
    relative_path: str = Field(description="Path relative to the workflow's artifact root.")
    content: str = Field(description="UTF-8 text content to write.")


class WriteWorkflowArtifactsArgs(BaseModel):
    workflow_id: str = Field(description="Workflow command id all artifacts belong to.")
    files: list[_WorkflowArtifactFile] = Field(
        description="List of {relative_path, content} entries. All-or-nothing: "
        "if any path is invalid, none are written. Max 20 files per call."
    )


class ListWorkflowArtifactsArgs(BaseModel):
    workflow_id: str = Field(description="Workflow command id whose artifact tree to list.")
    sub_path: str = Field(
        default="",
        description="Optional sub-directory under the artifact root to list (e.g. '003-feature').",
    )


class ReadRepoFileArgs(BaseModel):
    relative_path: str = Field(
        description="Path relative to the project repo root. Must live under specs/, "
        "stories/, reviews/, project_knowledge/, _bmad/, .cursor/commands/, .specify/, "
        "skills/ or docs/."
    )


class PublishArtifactsArgs(BaseModel):
    relative_paths: list[str] = Field(
        description=(
            "Project-root-relative paths to stage (e.g. ['specs/004-foo/spec.md']). "
            "Must already exist on disk and live under an agent-allowed root "
            "(for PM: specs/ stories/ reviews/ project_knowledge/ docs/ briefs/). "
            "Max 50 entries."
        ),
    )
    commit_message: str = Field(
        description=(
            "Commit message (max 4 KiB). Will be used verbatim. Be descriptive: "
            "'spec: <feature> initial draft' or 'doc: add <topic> research note'."
        )
    )
    remote: str = Field(
        default="origin",
        description="Git remote name. Defaults to 'origin'. No force push is ever performed.",
    )


class RunSpeckitScriptArgs(BaseModel):
    script: str = Field(
        description=(
            "Whitelisted script name from .specify/scripts/bash/, e.g. "
            "'create-new-feature.sh' (PM only) or 'setup-plan.sh' (TL only). "
            "The exact whitelist depends on the calling role."
        )
    )
    args: list[str] = Field(
        default_factory=list,
        description=(
            "Extra argv for the script, passed verbatim (no shell). "
            "Each entry is validated: ASCII word/punct/CJK only, no newlines, "
            "no path traversal, max 500 bytes, max 16 entries. For "
            "create-new-feature.sh use e.g. ['--json', '--short-name', "
            "'user-auth', 'Add user authentication system']."
        ),
    )


# ---------------------------------------------------------------------------
# Tool specs
# ---------------------------------------------------------------------------


_READ_WORKFLOW_INSTRUCTION_SPEC = _tool_spec(
    "read_workflow_instruction",
    "Read the methodology instruction file for a workflow command (e.g. speckit.plan, "
    "bmad:create-story, bmad:code-review, bmad:dev-story). Returns the instruction text "
    "you must follow plus artifact_subdir. Call this FIRST before producing any workflow "
    "artifact or running a bmad:* procedure.",
    ReadWorkflowInstructionArgs,
)

_LIST_WORKFLOW_ARTIFACTS_SPEC = _tool_spec(
    "list_workflow_artifacts",
    "List existing files/directories under a workflow's artifact root. Use before "
    "write_workflow_artifact to avoid colliding feature numbers, or to discover prior specs.",
    ListWorkflowArtifactsArgs,
)

_READ_REPO_FILE_SPEC = _tool_spec(
    "read_repo_file",
    "Read a UTF-8 text file from the project repo (e.g. specs/003-x/spec.md, "
    "stories/3-1-foo.md). Limited to safe roots; cannot read .env or secrets.",
    ReadRepoFileArgs,
)

_WRITE_WORKFLOW_ARTIFACT_SPEC = _tool_spec(
    "write_workflow_artifact",
    "Write a workflow artifact (spec.md, plan.md, tasks.md, story, review, ...) to the "
    "project repo under the workflow's artifact_subdir. Creates parent directories as needed.",
    WriteWorkflowArtifactArgs,
)

_WRITE_WORKFLOW_ARTIFACTS_SPEC = _tool_spec(
    "write_workflow_artifacts",
    "Batch version of write_workflow_artifact. Accepts ``files=[{relative_path, content}, ...]`` "
    "and writes them atomically under the workflow's artifact_subdir. Validation happens up-front: "
    "if ANY file fails validation (path escape, sensitive segment, bad types), NONE are written. "
    "Use this when speckit.plan / create-story produce multiple sibling artifacts.",
    WriteWorkflowArtifactsArgs,
)


_PUBLISH_ARTIFACTS_SPEC = _tool_spec(
    "publish_artifacts",
    "Commit the listed artifact files and push them to the remote on the current branch. "
    "Doc-only surface: each path must exist on disk AND live under an agent-allowed root "
    "(e.g. specs/ stories/ docs/). Staging is limited to the explicit path list — any "
    "pre-commit hook or unrelated file that appears in the index triggers EXTRA_STAGED_FILES "
    "and aborts. NEVER force-pushes. Use this after write_workflow_artifact when the user "
    "wants the doc available upstream (including direct commits to main / master for "
    "backlog notes / research docs).",
    PublishArtifactsArgs,
)


_RUN_SPECKIT_SCRIPT_SPEC = _tool_spec(
    "run_speckit_script",
    "Execute one of the whitelisted .specify/scripts/bash/*.sh scripts that the speckit "
    "command files require (e.g. create-new-feature.sh for speckit.specify, setup-plan.sh "
    "for speckit.plan). Args are passed as argv (no shell), validated against a strict "
    "character whitelist, and the script runs under the project repo root. The whitelist "
    "of script names is per-role; calling an out-of-scope script returns a "
    "SCRIPT_NOT_ALLOWED_FOR_AGENT error. When --json is in args, the JSON object printed "
    "on stdout is parsed into ``parsed_json`` (e.g. {BRANCH_NAME, SPEC_FILE, FEATURE_NUM}).",
    RunSpeckitScriptArgs,
)


# Read-only subset. Sub-agent roles (reviewer / developer / bug_fixer /
# sprint_planner / ux_designer / researcher) get this slice so they can
# LOAD bmad / speckit methodology files and browse existing artifacts,
# but they cannot produce workflow artifacts directly — those still go
# through tech_lead / prd_writer, who own the artifact lifecycle.
WORKFLOW_READ_TOOL_SPECS: list[AgentToolSpec] = [
    _READ_WORKFLOW_INSTRUCTION_SPEC,
    _LIST_WORKFLOW_ARTIFACTS_SPEC,
    _READ_REPO_FILE_SPEC,
]


# Full surface — for tech_lead and prd_writer.
WORKFLOW_TOOL_SPECS: list[AgentToolSpec] = [
    _READ_WORKFLOW_INSTRUCTION_SPEC,
    _WRITE_WORKFLOW_ARTIFACT_SPEC,
    _WRITE_WORKFLOW_ARTIFACTS_SPEC,
    _LIST_WORKFLOW_ARTIFACTS_SPEC,
    _READ_REPO_FILE_SPEC,
]


_WRITE_ONLY_TOOL_NAMES: frozenset[str] = frozenset(
    {"write_workflow_artifact", "write_workflow_artifacts"}
)


# ---------------------------------------------------------------------------
# Mixin
# ---------------------------------------------------------------------------


class WorkflowToolsMixin:
    """Mixin adding workflow tools to an executor.

    Host class must expose:
    - ``self._workflow: WorkflowService | None``
    - ``self._workflow_agent_name: str`` (``"product_manager"`` or ``"tech_lead"``
      for full surface; any role name for read-only sub-agents)
    - ``self.project_id: str``

    Optional:
    - ``self._workflow_readonly: bool`` (default ``False``). When ``True``,
      only ``read_workflow_instruction`` / ``list_workflow_artifacts`` /
      ``read_repo_file`` are exposed — the two write tools are stripped
      from ``tool_specs`` AND rejected at dispatch time. This lets
      sub-agent roles (reviewer / developer / bug_fixer / sprint_planner /
      ux_designer / researcher) LOAD bmad methodology files without
      gaining the ability to produce workflow artifacts, which still
      belong to tech_lead / prd_writer.
    """

    _workflow: WorkflowService | None
    _workflow_agent_name: str
    project_id: str
    # Opt-in read-only mode. Defaults to False so tech_lead / prd_writer
    # keep their existing full surface without touching anything.
    _workflow_readonly: bool = False

    def workflow_tool_specs(self) -> list[AgentToolSpec]:
        if self._workflow is None:
            return []
        if getattr(self, "_workflow_readonly", False):
            return list(WORKFLOW_READ_TOOL_SPECS)
        return list(WORKFLOW_TOOL_SPECS)

    async def handle_workflow_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Return a result dict if ``tool_name`` is a workflow tool, else None."""
        if self._workflow is None:
            return None
        # Belt + suspenders: even if a stale tool-call slipped through
        # the spec-filter for a readonly role, refuse writes at dispatch.
        if (
            getattr(self, "_workflow_readonly", False)
            and tool_name in _WRITE_ONLY_TOOL_NAMES
        ):
            return {
                "error": "WORKFLOW_WRITE_FORBIDDEN",
                "message": (
                    f"{tool_name} is not available to role "
                    f"'{self._workflow_agent_name}'. Workflow artifacts "
                    "are produced by tech_lead / prd_writer only. Use "
                    "write_role_artifact for your own findings if one "
                    "is wired in."
                ),
            }
        readonly = bool(getattr(self, "_workflow_readonly", False))
        try:
            if tool_name == "read_workflow_instruction":
                parsed = ReadWorkflowInstructionArgs.model_validate(arguments)
                return self._workflow.read_instruction(
                    parsed.workflow_id,
                    self._workflow_agent_name,
                    enforce_agent=not readonly,
                )
            if tool_name == "write_workflow_artifact":
                parsed = WriteWorkflowArtifactArgs.model_validate(arguments)
                return self._workflow.write_artifact(
                    workflow_id=parsed.workflow_id,
                    agent_name=self._workflow_agent_name,
                    project_id=self.project_id,
                    relative_path=parsed.relative_path,
                    content=parsed.content,
                )
            if tool_name == "write_workflow_artifacts":
                parsed_batch = WriteWorkflowArtifactsArgs.model_validate(arguments)
                return self._workflow.write_artifacts_batch(
                    workflow_id=parsed_batch.workflow_id,
                    agent_name=self._workflow_agent_name,
                    project_id=self.project_id,
                    files=[
                        {"relative_path": f.relative_path, "content": f.content}
                        for f in parsed_batch.files
                    ],
                )
            if tool_name == "list_workflow_artifacts":
                parsed = ListWorkflowArtifactsArgs.model_validate(arguments)
                return self._workflow.list_artifacts(
                    workflow_id=parsed.workflow_id,
                    agent_name=self._workflow_agent_name,
                    project_id=self.project_id,
                    sub_path=parsed.sub_path,
                    enforce_agent=not readonly,
                )
            if tool_name == "read_repo_file":
                parsed = ReadRepoFileArgs.model_validate(arguments)
                return self._workflow.read_repo_file(
                    project_id=self.project_id,
                    relative_path=parsed.relative_path,
                )
        except WorkflowError as exc:
            return {"error": exc.code, "message": exc.message}
        return None


# ---------------------------------------------------------------------------
# Speckit script mixin
# ---------------------------------------------------------------------------


class SpeckitScriptMixin:
    """Adds the ``run_speckit_script`` tool to an executor.

    Host class must expose:
    - ``self._speckit_scripts: SpeckitScriptService | None``
    - ``self._workflow_agent_name: str`` (also required by
      ``WorkflowToolsMixin``, kept consistent so a single role name
      drives both per-workflow and per-script ACLs).
    - ``self.project_id: str``

    The tool is omitted from ``tool_specs`` when the service is not
    wired OR when the calling role has no scripts whitelisted at all
    — there is no point advertising a tool the agent can never use
    successfully.
    """

    _speckit_scripts: SpeckitScriptService | None
    _workflow_agent_name: str
    project_id: str

    def speckit_script_tool_specs(self) -> list[AgentToolSpec]:
        if self._speckit_scripts is None:
            return []
        if not self._speckit_scripts.allowed_scripts_for_agent(
            self._workflow_agent_name
        ):
            return []
        return [_RUN_SPECKIT_SCRIPT_SPEC]

    async def handle_speckit_script_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Return a result dict if ``tool_name`` is the speckit tool, else None."""
        if tool_name != "run_speckit_script":
            return None
        if self._speckit_scripts is None:
            return {
                "error": "SPECKIT_SCRIPT_SERVICE_UNAVAILABLE",
                "message": (
                    "Speckit script service is not wired for this bot; "
                    "ask the operator to enable it before retrying."
                ),
            }
        try:
            parsed = RunSpeckitScriptArgs.model_validate(arguments)
        except Exception as exc:  # pydantic ValidationError, kept broad
            return {
                "error": "SPECKIT_SCRIPT_BAD_ARGS",
                "message": f"Invalid arguments: {exc}",
            }
        try:
            result = self._speckit_scripts.run_script(
                agent_name=self._workflow_agent_name,
                project_id=self.project_id,
                script=parsed.script,
                args=list(parsed.args or []),
            )
        except SpeckitScriptError as exc:
            return {"error": exc.code, "message": exc.message}
        return {
            "script": result.script,
            "argv": list(result.argv),
            "exit_code": result.exit_code,
            "success": result.success,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "parsed_json": result.parsed_json,
            "elapsed_ms": result.elapsed_ms,
        }


# ---------------------------------------------------------------------------
# Artifact publish mixin
# ---------------------------------------------------------------------------


class ArtifactPublishMixin:
    """Adds the ``publish_artifacts`` tool to an executor.

    Host class must expose:
    - ``self._artifact_publish: ArtifactPublishService | None``
    - ``self._workflow_agent_name: str``
    - ``self.project_id: str``

    The tool is omitted from ``tool_specs`` when the service is not
    wired OR the calling role has no publish roots whitelisted.
    """

    _artifact_publish: ArtifactPublishService | None
    _workflow_agent_name: str
    project_id: str

    def artifact_publish_tool_specs(self) -> list[AgentToolSpec]:
        if self._artifact_publish is None:
            return []
        if not self._artifact_publish.is_agent_enabled(self._workflow_agent_name):
            return []
        return [_PUBLISH_ARTIFACTS_SPEC]

    async def handle_artifact_publish_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        if tool_name != "publish_artifacts":
            return None
        if self._artifact_publish is None:
            return {
                "error": "ARTIFACT_PUBLISH_UNAVAILABLE",
                "message": (
                    "Artifact publish service not wired for this bot; ask "
                    "the operator to enable it before retrying."
                ),
            }
        try:
            parsed = PublishArtifactsArgs.model_validate(arguments)
        except Exception as exc:
            return {
                "error": "ARTIFACT_PUBLISH_BAD_ARGS",
                "message": f"Invalid arguments: {exc}",
            }
        try:
            result = self._artifact_publish.publish(
                agent_name=self._workflow_agent_name,
                project_id=self.project_id,
                relative_paths=list(parsed.relative_paths or []),
                commit_message=parsed.commit_message,
                remote=parsed.remote,
            )
        except ArtifactPublishError as exc:
            return {"error": exc.code, "message": exc.message}
        return {
            "project_id": result.project_id,
            "branch": result.branch,
            "commit_sha": result.commit_sha,
            "commit_message": result.commit_message,
            "paths": list(result.paths),
            "remote": result.remote,
            "pushed": result.pushed,
            "push_output": result.push_output,
            "elapsed_ms": result.elapsed_ms,
        }
