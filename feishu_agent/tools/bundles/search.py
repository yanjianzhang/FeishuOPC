"""Bundle: ``search``

Tool surface (workflow-read semantics — see note below):
- ``read_workflow_instruction`` (effect=read, target=read.workflow)
- ``list_workflow_artifacts`` (effect=read, target=read.specs)
- ``read_repo_file`` (effect=read, target=read.repo)

Name / semantics note
---------------------
The A-2 initial inventory labelled this bundle ``search`` with hypothetical
``search_repo`` / ``search_specs`` tools. No such grep-style tools exist in
the MVP; however the 7 roles migrated in Wave 3 (researcher / sprint_planner
/ ux_designer / ...) already depend on three read-only workflow tools that
do the same job in practice:

- ``read_workflow_instruction`` → "find methodology" (≈ search for guidance)
- ``list_workflow_artifacts`` → "find existing specs/plans" (≈ search specs)
- ``read_repo_file`` → "read a spec / stories / reviews file"

Rather than proliferate a parallel ``workflow_read`` bundle, we keep the
spec-mandated bundle name and populate it with the actual search-surface
the MVP supports. A future grep-style ``search_repo`` tool can be added
to this bundle without churning role frontmatter.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.tools.bundle_context import BundleContext
from feishu_agent.tools.bundle_registry import Handler
from feishu_agent.tools.workflow_service import WorkflowError
from feishu_agent.tools.workflow_tools import (
    WORKFLOW_READ_TOOL_SPECS,
    ListWorkflowArtifactsArgs,
    ReadRepoFileArgs,
    ReadWorkflowInstructionArgs,
)

_TARGET_BY_NAME: dict[str, str] = {
    "read_workflow_instruction": "read.workflow",
    "list_workflow_artifacts": "read.specs",
    "read_repo_file": "read.repo",
}


def build_search_bundle(
    ctx: BundleContext,
) -> list[tuple[AgentToolSpec, Handler]]:
    workflow = ctx.workflow_service
    if workflow is None:
        return []

    role_name = ctx.role_name
    project_id = ctx.project_id
    items: list[tuple[AgentToolSpec, Handler]] = []

    for base_spec in WORKFLOW_READ_TOOL_SPECS:
        target = _TARGET_BY_NAME.get(base_spec.name, "read.specs")
        spec = replace(base_spec, effect="read", target=target)

        if base_spec.name == "read_workflow_instruction":

            def _handle_instruction(arguments: dict[str, Any]) -> dict[str, Any]:
                parsed = ReadWorkflowInstructionArgs.model_validate(arguments)
                try:
                    return workflow.read_instruction(
                        parsed.workflow_id,
                        role_name,
                        enforce_agent=False,
                    )
                except WorkflowError as exc:
                    return {"error": exc.code, "message": exc.message}

            items.append((spec, _handle_instruction))

        elif base_spec.name == "list_workflow_artifacts":

            def _handle_list(arguments: dict[str, Any]) -> dict[str, Any]:
                parsed = ListWorkflowArtifactsArgs.model_validate(arguments)
                try:
                    return workflow.list_artifacts(
                        workflow_id=parsed.workflow_id,
                        agent_name=role_name,
                        project_id=project_id,
                        sub_path=parsed.sub_path,
                        enforce_agent=False,
                    )
                except WorkflowError as exc:
                    return {"error": exc.code, "message": exc.message}

            items.append((spec, _handle_list))

        elif base_spec.name == "read_repo_file":

            def _handle_read_repo(arguments: dict[str, Any]) -> dict[str, Any]:
                parsed = ReadRepoFileArgs.model_validate(arguments)
                try:
                    return workflow.read_repo_file(
                        project_id=project_id,
                        relative_path=parsed.relative_path,
                    )
                except WorkflowError as exc:
                    return {"error": exc.code, "message": exc.message}

            items.append((spec, _handle_read_repo))

    return items
