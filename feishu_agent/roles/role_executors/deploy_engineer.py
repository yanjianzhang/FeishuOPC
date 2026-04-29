"""Deploy engineer role executor.

Extracted from ``tech_lead_executor.py`` (commit history pre-split).
The tech lead used to own ``deploy_project`` + ``describe_deploy_project``
directly; that made TL's tool surface wider and forced its skill file
to carry a full deploy workflow block. The deploy path is mechanical
(describe → pick flag → run → classify), so lifting it into its own
dispatched role keeps TL slim without moving any wire-level logic.

The executor is intentionally tiny: two tool handlers + constructor-
injected :class:`DeployService`. ``role_name`` defaults to
``deploy_engineer`` so audit logs line up with the role registry entry.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from pydantic import BaseModel, Field

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.tools.deploy_service import (
    DeployError,
    DeployNotConfiguredError,
    DeployResult,
    DeployService,
    UnknownProjectError,
)
from feishu_agent.tools.feishu_agent_tools import _tool_spec

logger = logging.getLogger(__name__)


def _format_duration_ms(ms: int) -> str:
    """Pretty-print a duration in ms as ``Mm Ss`` / ``Ss``.

    Rounding: any non-zero millisecond fraction is rounded **up** to the
    next whole second via :func:`math.ceil` so a 500 ms job renders as
    ``"1s"`` rather than ``"0s"`` — matching the intuition that a
    finished-but-sub-second deploy still took "a second". Negative
    inputs are clamped to zero.

    Duplicated (copy, not import) from the pre-split
    ``tech_lead_executor.py`` helper so this module has no inbound
    dependency on the tech lead executor. The function is trivial and
    deploy-only; no other caller exists.
    """

    total_sec = max(0, math.ceil(ms / 1000))
    if total_sec < 60:
        return f"{total_sec}s"
    m, s = divmod(total_sec, 60)
    return f"{m}m {s}s"


class DeployProjectArgs(BaseModel):
    """Arguments for ``deploy_project``.

    ``deploy_project`` runs THIS project's ``deploy/deploy.sh``
    (whatever that entrypoint does — rsync web, rebuild container,
    restart service, etc.). FeishuOPC is only the caller; the script
    lives in the project repo and owns server topology, secrets,
    build steps. See ``docs/deploy-convention.md``.

    The user's plain-language request ("部署一下 / 上 prod / 推到
    服务器") is the confirmation — this tool does NOT gate through
    ``request_confirmation``. The trade-off: a stray dispatch costs
    a deploy cycle. Mitigation: only ``deploy_engineer`` can invoke,
    and the service logs every run to ``.larkagent/logs/deploy/`` so
    an accidental deploy is auditable.
    """

    args: list[str] = Field(
        default_factory=list,
        description=(
            "Pass-through flags / positional args for the project's "
            "deploy.sh. Read ``deploy/README.md`` (via read_repo_file) "
            "to learn which flags the project supports. Common examples: "
            "``[]`` (full deploy), ``['--setup']`` (first-time provisioning), "
            "``['--server-only']`` / ``['--web-only']`` / ``['--rollback']`` "
            "— whatever the project documents. FeishuOPC applies argv-safety "
            "checks (no shell metachars, no newlines) but does NOT validate "
            "semantics; unknown flags just get passed to the script."
        ),
    )
    timeout_seconds: int | None = Field(
        default=None,
        description=(
            "Max seconds to let deploy.sh run. Default: 1800 (30 min). "
            "Hard-capped at 3600. Reduce when you know the deploy is "
            "small (e.g. web-only push); do NOT raise unless you have "
            "a reason — a genuine deploy that takes >1h should be split "
            "by the project into server-only / web-only phases."
        ),
        ge=1,
    )


_DEPLOY_PROJECT_SPEC = _tool_spec(
    "deploy_project",
    "Run THIS project's deploy script (path declared in FeishuOPC's "
    "``deploy_projects/<pid>.json::script_path``, usually "
    "``deploy/deploy.sh``) to push server / web / DB artifacts to the "
    "project's production server. Server credentials live in the PROJECT "
    "repo (``deploy/secrets/server.env`` or wherever the script reads "
    "them from) — FeishuOPC never sees them. Full log lands at "
    "``.larkagent/logs/deploy/<project>-<ts>.log``; the tool returns "
    "``exit_code`` / ``success`` / ``stdout_tail`` / ``stderr_tail`` / "
    "``elapsed_ms`` / ``log_path`` / ``command`` / ``script_path`` so "
    "you can summarize to the dispatcher or hand the log to bug_fixer "
    "if it failed. Call ``describe_deploy_project`` first to see which "
    "flags this project supports.",
    DeployProjectArgs,
)


class DescribeDeployProjectArgs(BaseModel):
    """Arguments for ``describe_deploy_project``.

    No args — the tool always introspects the current session's
    ``project_id``. Kept as a model (rather than raw empty object) so
    the tool-spec machinery can generate a stable JSON schema the LLM
    sees as ``{"type": "object", "properties": {}}``.
    """


_DESCRIBE_DEPLOY_PROJECT_SPEC = _tool_spec(
    "describe_deploy_project",
    "Introspect THIS project's deploy metadata from FeishuOPC's "
    "``.larkagent/secrets/deploy_projects/<pid>.json``: the script path, "
    "the catalog of supported flags (with Chinese/English descriptions "
    "the operator wrote for you), default args, default timeout, and "
    "free-form notes. Call this BEFORE ``deploy_project`` to decide "
    "which flag matches the user's intent ('部署' → likely ``[]`` / "
    "full; '只发后端' → ``['--server-only']`` if that flag exists; etc.). "
    "Does NOT read the project repo filesystem — the catalog is "
    "FeishuOPC-side metadata, so no ``allowed_read_roots`` dance. Returns "
    "``{project_id, script_path, script_absolute, script_exists, "
    "default_args, supported_flags, default_timeout_seconds, "
    "max_timeout_seconds, notes}``.",
    DescribeDeployProjectArgs,
)


DEPLOY_ENGINEER_TOOL_SPECS: list[AgentToolSpec] = [
    _DESCRIBE_DEPLOY_PROJECT_SPEC,
    _DEPLOY_PROJECT_SPEC,
]


class DeployEngineerExecutor:
    """AgentToolExecutor for the ``deploy_engineer`` role.

    Tools: ``describe_deploy_project``, ``deploy_project``.

    The role is dispatched by the tech lead when the user asks to
    deploy. It does not own any persistent state — the verdict it
    returns (``success | code_failure | env_failure | unclear |
    config_error``) is computed by the caller from the structured
    tool return values this executor produces. We deliberately do NOT
    push Feishu thread updates from here because the dispatch
    machinery already emits role-start / role-end messages; adding
    deploy-level updates would double-post.
    """

    def __init__(
        self,
        *,
        deploy_service: DeployService | None = None,
        project_id: str = "",
        command_text: str = "",
        role_name: str = "deploy_engineer",
        trace_id: str = "",
        chat_id: str | None = None,
        **_kwargs: Any,
    ) -> None:
        self._deploy_service = deploy_service
        self.project_id = project_id
        self.command_text = command_text
        self.role_name = role_name
        self.trace_id = trace_id
        self.chat_id = chat_id

    def tool_specs(self) -> list[AgentToolSpec]:
        return list(DEPLOY_ENGINEER_TOOL_SPECS)

    async def execute_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | list[Any] | str:
        if tool_name == "describe_deploy_project":
            DescribeDeployProjectArgs.model_validate(arguments)
            return await self._describe_deploy_project()
        if tool_name == "deploy_project":
            parsed = DeployProjectArgs.model_validate(arguments)
            return await self._deploy_project(parsed)
        raise RuntimeError(f"Unsupported tool: {tool_name}")

    async def _describe_deploy_project(self) -> dict[str, Any]:
        """Return FeishuOPC-side deploy metadata for the current project.

        Pure read from FeishuOPC's own filesystem — does NOT touch the
        project repo, does NOT go through ``allowed_read_roots``, does
        NOT require the project to ship a ``deploy/README.md``. All the
        LLM needs to pick a flag (description, expected duration hint)
        lives in the JSON the operator wrote.
        """
        if self._deploy_service is None:
            return {
                "ok": False,
                "error": "DEPLOY_SERVICE_DISABLED",
                "message": (
                    "No DeployService is wired in this runtime."
                ),
            }
        project_id = (self.project_id or "").strip()
        if not project_id:
            return {
                "ok": False,
                "error": "UNKNOWN_PROJECT",
                "message": (
                    "No project_id is bound to this dispatch."
                ),
            }
        try:
            info = self._deploy_service.describe(project_id)
        except (UnknownProjectError, DeployNotConfiguredError) as exc:
            return {
                "ok": False,
                "error": exc.code,
                "message": exc.message,
                "project_id": project_id,
            }
        return {"ok": True, **info}

    async def _deploy_project(
        self, args: DeployProjectArgs
    ) -> dict[str, Any]:
        """Run the current project's ``deploy/deploy.sh``.

        Structured return keeps the dispatcher in control:
        - ``success=true`` → caller summarizes duration and moves on.
        - ``success=false`` → caller surfaces ``stderr_tail`` + ``log_path``
          and may route to bug_fixer to read the log.
        - ``error`` key present → categorical failure (missing script,
          timeout, arg rejected). Caller reads the code and acts
          accordingly.
        """
        if self._deploy_service is None:
            return {
                "ok": False,
                "error": "DEPLOY_SERVICE_DISABLED",
                "message": (
                    "No DeployService is wired in this runtime — "
                    "deploy_project is unavailable. Check the server "
                    "config."
                ),
            }

        project_id = (self.project_id or "").strip()
        if not project_id:
            return {
                "ok": False,
                "error": "UNKNOWN_PROJECT",
                "message": (
                    "No project_id is bound to this dispatch, so "
                    "deploy_project has nothing to deploy."
                ),
            }

        try:
            result: DeployResult = await asyncio.to_thread(
                self._deploy_service.run,
                agent_name=self.role_name,
                project_id=project_id,
                args=list(args.args),
                timeout_seconds=args.timeout_seconds,
            )
        except DeployError as exc:
            return {
                "ok": False,
                "error": exc.code,
                "message": exc.message,
                "project_id": project_id,
            }
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception("deploy_project unexpected failure")
            return {
                "ok": False,
                "error": "DEPLOY_UNEXPECTED_FAILURE",
                "message": str(exc),
                "project_id": project_id,
            }

        return {
            "ok": True,
            "success": result.success,
            "exit_code": result.exit_code,
            "elapsed_ms": result.elapsed_ms,
            "elapsed_human": _format_duration_ms(result.elapsed_ms),
            "project_id": result.project_id,
            "command": result.command,
            "argv": list(result.argv),
            "log_path": result.log_path,
            "stdout_tail": result.stdout_tail,
            "stderr_tail": result.stderr_tail,
        }
