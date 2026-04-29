from __future__ import annotations

import inspect
import json
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field

from feishu_agent.core.agent_types import (
    AgentToolExecutor,
    AgentToolSpec,
    AllowListedToolExecutor,
)
from feishu_agent.core.llm_agent_adapter import LlmAgentAdapter
from feishu_agent.roles.role_executors.tool_handlers import read_sprint_status
from feishu_agent.roles.role_registry_service import (
    RoleDefinition,
    RoleNotFoundError,
    RoleRegistryService,
)
from feishu_agent.runtime.impersonation_token_service import ImpersonationTokenService
from feishu_agent.runtime.managed_feishu_client import ManagedFeishuClient
from feishu_agent.team.agent_notes_service import (
    AgentNoteError,
    AgentNotesService,
)
from feishu_agent.team.artifact_store import (
    ARGS_PREVIEW_MAX,
    OUTPUT_TEXT_MAX,
    RESULT_PREVIEW_MAX,
    ArtifactStore,
    FileTouch,
    RoleArtifact,
    ToolCallRecord,
    compute_risk_score,
    truncate_preview,
)
from feishu_agent.team.audit_service import AuditService
from feishu_agent.team.pending_action_service import PendingAction, PendingActionService
from feishu_agent.team.sprint_state_service import SprintStateService
from feishu_agent.team.task_graph import (
    ClaimConflictError,
    ClaimOwnershipError,
    TaskGraph,
    TaskNotFoundError,
)
from feishu_agent.team.task_service import TaskHandle
from feishu_agent.team.worktree_manager import WorktreeHandle, WorktreeManager
from feishu_agent.tools.ci_watch_service import CIWatchService
from feishu_agent.tools.code_write_service import CodeWriteService
from feishu_agent.tools.code_write_tools import CodeWriteToolsMixin
from feishu_agent.tools.feishu_agent_tools import (
    TECH_LEAD_TOOL_SPECS,
    AdvanceSprintStateArgs,
    RequestConfirmationArgs,
    SprintArgs,
    _tool_spec,
)
from feishu_agent.tools.git_ops_service import GitOpsService
from feishu_agent.tools.pre_push_inspector import PrePushInspector
from feishu_agent.tools.progress_sync_service import ProgressSyncService
from feishu_agent.tools.pull_request_service import PullRequestService
from feishu_agent.tools.speckit_script_service import SpeckitScriptService
from feishu_agent.tools.workflow_service import WorkflowService
from feishu_agent.tools.workflow_tools import (
    SpeckitScriptMixin,
    WorkflowToolsMixin,
)

# Story 004.5 — the provider now accepts an optional ``working_dir``
# override so TL can hand it a git worktree path (B-3). The override is
# keyword-only so existing two-arg callers (e.g. the A-2 Wave 3 legacy
# tests that call ``provider(role_name, role)`` positionally) keep
# working unchanged. Kept as ``Callable[..., ...]`` rather than a
# strict three-arg Protocol to avoid churning every test fixture.
RoleExecutorProvider = Callable[..., AgentToolExecutor | None]

if TYPE_CHECKING:
    from feishu_agent.core.cancel_token import CancelToken
    from feishu_agent.core.hook_bus import HookBus
    from feishu_agent.runtime.feishu_runtime_service import ThreadUpdateFn

logger = logging.getLogger(__name__)


# Error payloads that the LLM routinely self-corrects from — we do NOT
# want to spam the Feishu thread with ⚠️ notifications for them because
# (a) they are non-actionable for the human user and (b) the same turn
# almost always ends with a successful retry. These are still written
# to the agent log for debugging via the observer fallback.
#
# - ``TOOL_CALL_ARG_MISSING`` — pydantic caught a missing field on the
#   tool call; the adapter already rewrites the error into a recovery
#   hint for the model.
# - ``TOOL_NOT_ALLOWED_ON_ROLE`` / ``TOOL_NOT_ALLOWED`` — the sub-agent
#   attempted a tool outside its allow-list. The executor short-
#   circuits with a structured error and the model picks a different
#   tool.
# - ``UNKNOWN_WORKFLOW`` — the model guessed a workflow_id prefix
#   (``bmm:`` vs ``bmad:``) that does not resolve. We now accept both
#   prefixes upstream, but older sessions still trip this path.
# - ``"Unsupported tool: "`` — same category as TOOL_NOT_ALLOWED, but
#   raised as a RuntimeError from ``AgentToolExecutor.execute_tool``
#   and wrapped into ``{"error": str(exc)}`` by the adapter.
_NOISY_ERROR_CODES: frozenset[str] = frozenset(
    {
        "TOOL_CALL_ARG_MISSING",
        "TOOL_NOT_ALLOWED_ON_ROLE",
        "TOOL_NOT_ALLOWED",
        "UNKNOWN_WORKFLOW",
    }
)
_NOISY_ERROR_PREFIXES: tuple[str, ...] = (
    "Unsupported tool:",
    "Unknown workflow_id:",
    "TOOL_NOT_ALLOWED:",
    "TOOL_NOT_ALLOWED_ON_ROLE:",
    "UNKNOWN_WORKFLOW:",
)


def _extract_file_touches(
    tool_name: str,
    kind: str,
    args: dict[str, Any],
    result: Any,
) -> list[FileTouch]:
    """Project a code-writing tool call into one or more
    :class:`FileTouch` stubs (A-3 / OQ-004-5 placeholder).

    We intentionally derive the ``path`` list from the *call's
    arguments* rather than from the result, because a successful
    batch write returns an aggregate summary dict while the
    arguments carry the individual paths. ``bytes_written`` comes
    from the result when available; we tolerate missing keys and
    leave the field as ``None`` rather than guessing.
    """

    touches: list[FileTouch] = []

    if tool_name == "write_project_code_batch":
        # Expected args shape: {"writes": [{"path": ..., "content": ...}, ...]}
        writes = args.get("writes") or args.get("files") or []
        if isinstance(writes, list):
            for entry in writes:
                if not isinstance(entry, dict):
                    continue
                path = entry.get("path") or entry.get("relative_path")
                if not path:
                    continue
                content = entry.get("content") or ""
                bytes_written = (
                    len(content.encode("utf-8"))
                    if isinstance(content, str)
                    else None
                )
                touches.append(
                    FileTouch(
                        path=str(path),
                        kind=kind,
                        bytes_written=bytes_written,
                    )
                )
        return touches

    # Single-file writers: write_project_code, write_role_artifact,
    # write_file, delete_project_code.
    path = (
        args.get("path")
        or args.get("relative_path")
        or args.get("file_path")
    )
    if not path:
        return touches
    bytes_written: int | None = None
    if kind != "delete":
        content = args.get("content") or ""
        if isinstance(content, str):
            bytes_written = len(content.encode("utf-8"))
        # Some write tools (e.g. write_role_artifact) return
        # ``{"bytes_written": N}`` in the result — prefer that
        # when present because it reflects what actually landed
        # on disk after any service-side normalisation.
        if isinstance(result, dict):
            served = result.get("bytes_written")
            if isinstance(served, int) and served >= 0:
                bytes_written = served
    touches.append(FileTouch(path=str(path), kind=kind, bytes_written=bytes_written))
    return touches


def _is_noisy_tool_error(result: Any) -> bool:
    """True when the tool error is an LLM-self-correctable fast-fail.

    The observer uses this to decide whether to emit a ⚠️ Feishu
    thread notification. Noisy errors are still sent back to the
    model (so it can retry), we just don't pester the human user.
    """

    if not isinstance(result, dict):
        return False
    err = result.get("error")
    if err is None:
        return False
    # Structured-code shape: {"error": "TOOL_CALL_ARG_MISSING", ...}
    if isinstance(err, str):
        if err in _NOISY_ERROR_CODES:
            return True
        if any(err.startswith(p) for p in _NOISY_ERROR_PREFIXES):
            return True
    return False


class DispatchRoleAgentArgs(BaseModel):
    role_name: str = Field(description="Role agent to dispatch (e.g. 'sprint_planner', 'repo_inspector')")
    task: str = Field(description="Natural language task description for the role agent")
    acceptance_criteria: str = Field(default="", description="Expected output format or acceptance criteria")
    workflow_id: str = Field(
        default="",
        description=(
            "Optional BMAD / speckit workflow id to pin the sub-agent to. "
            "When set (e.g. 'bmad:code-review', 'bmad:dev-story', "
            "'bmad:correct-course', 'bmad:sprint-planning'), the sub-agent "
            "is reminded in its prompt to call "
            "read_workflow_instruction(<workflow_id>) FIRST, so it follows "
            "the canonical methodology from _bmad/ instead of reconstructing "
            "it from the role system prompt. Sub-agents that carry the "
            "read-only WorkflowToolsMixin (reviewer / developer / bug_fixer / "
            "sprint_planner / ux_designer / researcher) honor this hint."
        ),
    )
    concurrency_group: str = Field(
        default="",
        description=(
            "B-2 effect-aware fan-out: override the default "
            "per-role concurrency group. Leave empty to use "
            "``role_name`` (the safe default — two concurrent "
            "dispatches of the same role always serialise). "
            "Set to a custom label to force serialisation with "
            "another dispatch that shares a downstream resource "
            "(e.g. all roles that write to the same Bitable)."
        ),
    )
    task_id: str = Field(
        default="",
        description=(
            "Story 004.5 / B-1 claim wiring: optional id of a task "
            "in the sprint-status ``tasks:`` block. When set AND the "
            "TL has a TaskGraph wired, the dispatch takes a claim "
            "lease on that task for the duration of the child session "
            "(pending → in-progress), and releases/completes it on "
            "exit. If the task is already claimed by someone else or "
            "is not pending, the claim fails and the dispatch proceeds "
            "without a claim — the LLM receives a warning in the "
            "result payload. Leave empty when the dispatch is not "
            "tracked in the task graph (most dispatches today)."
        ),
    )


class DelegateToApplicationAgentArgs(BaseModel):
    message: str = Field(
        description="Bitable / Feishu task body for the application assistant. "
        "Delivery uses the delegate webhook when configured; otherwise Feishu group IM with @mention "
        "of the application assistant. Use clear Chinese."
    )


class AppendAgentNoteArgs(BaseModel):
    # Capped at ~512 chars (``AgentNotesService.MAX_NOTE_CHARS``) — mirrored
    # in the tool description so the LLM self-enforces. The service does
    # enforce it too; this is a hint, not a gate.
    note: str = Field(
        description=(
            "A short (≤512 chars) note worth remembering across Feishu "
            "sessions for THIS project. Examples: architectural decisions the "
            "user approved, branch naming conventions, gotchas in the repo, "
            "dependencies that must stay pinned. AVOID: chit-chat, one-off "
            "statuses, anything that changes often. Notes land in "
            "``AGENT_NOTES.md`` at the project root and are injected into "
            "the next session's system prompt."
        ),
        min_length=1,
    )


_DISPATCH_ROLE_AGENT_SPEC = _tool_spec(
    "dispatch_role_agent",
    "Dispatch a role agent to perform a specific task. The agent runs in isolation with its own tool set.",
    DispatchRoleAgentArgs,
)


class ResumeLastDispatchArgs(BaseModel):
    """Arguments for ``resume_last_dispatch``.

    The handler scans this thread's event log for the most recent
    ``dispatch_role_agent`` call, reads its original args off the
    log, inspects the workflow's artifact directory for anything the
    previous sub-agent wrote before it died, and re-dispatches the
    SAME role with a task that tells the new sub-agent to READ the
    partial(s) first and continue from there instead of starting
    over. Pure quality-of-life wrapper: it never invents a dispatch
    that the TL didn't already make in this thread.
    """

    extra_context: str = Field(
        default="",
        description=(
            "Optional Chinese natural-language note appended to the "
            "resumed task. Use this to tell the sub-agent what went "
            "wrong last time (e.g. 'Tool loop 超时 at 177s') or to "
            "narrow the scope of the retry. Leave empty when you "
            "just want a vanilla pick-up-where-you-left-off."
        ),
    )
    force: bool = Field(
        default=False,
        description=(
            "By default the tool refuses to resume a dispatch whose "
            "last result was a success — there is nothing to recover. "
            "Set ``force=True`` to re-dispatch anyway (e.g. the user "
            "explicitly wants another pass)."
        ),
    )


_RESUME_LAST_DISPATCH_SPEC = _tool_spec(
    "resume_last_dispatch",
    "Re-dispatch the most recent sub-agent (reviewer / developer / …) in "
    "THIS thread, telling it to read whatever partial artifacts the "
    "previous run left on disk and continue from there. Use when the "
    "last `dispatch_role_agent` timed out / failed mid-loop and you "
    "want to pick up without having to retype the task description. "
    "Only works when there is a prior `dispatch_role_agent` call in "
    "this thread's event log.",
    ResumeLastDispatchArgs,
)

_REQUEST_CONFIRMATION_SPEC = _tool_spec(
    "request_confirmation",
    "Request human confirmation before executing a destructive, hard-to-undo action "
    "(currently: `write_progress_sync`, which pushes rows into Feishu Bitable). "
    "Do NOT gate `advance_sprint_state` through this tool — just call "
    "`advance_sprint_state` directly; sprint-status.yaml flips are reversible and "
    "fully audit-logged. Call `request_confirmation` INSTEAD of the destructive "
    "action; the user will see the summary and reply 确认 / 取消.",
    RequestConfirmationArgs,
)

_DELEGATE_TO_APP_AGENT_SPEC = _tool_spec(
    "delegate_to_application_agent",
    "Delegate Feishu Bitable read/write to the application assistant. "
    "If APPLICATION_AGENT_DELEGATE_URL is set, POST JSON to that ingest URL (assistant confirms in Feishu separately); "
    "otherwise send a text message to APPLICATION_AGENT_GROUP_CHAT_ID via IM API with an @mention "
    "of APPLICATION_AGENT_OPEN_ID. Use clear Chinese in `message`.",
    DelegateToApplicationAgentArgs,
)

_APPEND_AGENT_NOTE_SPEC = _tool_spec(
    "append_agent_note",
    "Append a durable note about THIS project to AGENT_NOTES.md. Use this "
    "for decisions / conventions / gotchas the next Feishu session should "
    "remember. Quota: ~5 notes per session. Do NOT store secrets, tokens, "
    "or transient statuses — those belong in the thread, not memory.",
    AppendAgentNoteArgs,
)


TECH_LEAD_V2_TOOL_SPECS = [
    *TECH_LEAD_TOOL_SPECS,
    _DISPATCH_ROLE_AGENT_SPEC,
    _RESUME_LAST_DISPATCH_SPEC,
    _REQUEST_CONFIRMATION_SPEC,
    _DELEGATE_TO_APP_AGENT_SPEC,
    _APPEND_AGENT_NOTE_SPEC,
]


# Minimum per-dispatch sub-agent budget. If the tech lead's remaining
# overall budget cannot cover at least this many seconds, the
# dispatch is rejected with ``OUT_OF_BUDGET`` instead of handing the
# sub-agent an unworkable timeout. Empirically a developer / reviewer
# session needs at least one full LLM round-trip (30–60s on the
# Anthropic relay) plus one or two tool calls to do anything useful;
# 120s is the smallest amount that gives them a real chance. This
# pins the 2026-04-20 incident where reviewer 2 was dispatched with
# a 40s budget inherited from a near-exhausted TL — it failed at
# 40.0s without producing any artifact, wasting a TL turn and
# confusing the user with a cryptic "Tool loop timed out" message.
MIN_SUB_AGENT_TIMEOUT_SECONDS: int = 120


# Tech lead's code-related surface is READ + GATEKEEPER.
# It no longer types code — that's the developer role's job. TL still
# needs to read the repo (to understand what to inspect/review) and
# owns every mutation that reaches the remote (inspection → commit
# fix-up → push → PR). ``git_commit`` stays on TL for last-mile
# tweaks (e.g. applying an inspector-flagged fix); bulk code goes
# through the developer.
TECH_LEAD_CODE_WRITE_ALLOW: frozenset[str] = frozenset(
    {
        "describe_code_write_policy",
        "read_project_code",
        "list_project_paths",
        "run_pre_push_inspection",
        "git_sync_remote",
        # TL is the only role that can carve off a fresh work branch
        # from origin/main — developer / bug_fixer / reviewer can't.
        # This keeps branch hygiene centralized and reviewable through
        # TL's audit trail.
        "start_work_branch",
        "git_commit",
        "git_push",
        "create_pull_request",
        # CI watch is the post-PR gate: TL is the only role allowed to
        # block on GitHub Actions before declaring "PR ready to merge".
        # Developer / reviewer / bug_fixer must NOT watch — they would
        # leak the gate decision into a sub-agent that can't escalate
        # back to the user.
        "watch_pr_checks",
    }
)


class TechLeadToolExecutor(WorkflowToolsMixin, CodeWriteToolsMixin, SpeckitScriptMixin):
    """AgentToolExecutor for the TechLead.

    Tools: read_sprint_status, advance_sprint_state, dispatch_role_agent,
    request_confirmation, delegate_to_application_agent; plus (when wired)
    four workflow tools from ``WorkflowToolsMixin``; plus project-code
    READ + GIT GATEKEEPER tools from ``CodeWriteToolsMixin``.

    Tech lead no longer has ``write_project_code`` or
    ``write_project_code_batch`` — those belong to the ``developer``
    role. Dispatch → read implementation note → inspect → commit (if
    needed) → push → PR.
    """

    def __init__(
        self,
        *,
        progress_sync_service: ProgressSyncService,
        sprint_state_service: SprintStateService,
        audit_service: AuditService,
        llm_agent_adapter: LlmAgentAdapter,
        role_registry: RoleRegistryService,
        pending_action_service: PendingActionService | None = None,
        feishu_client: ManagedFeishuClient | None = None,
        application_agent_open_id: str = "",
        application_agent_group_chat_id: str = "",
        application_agent_delegate_url: str | None = None,
        application_agent_display_name: str = "Application delegate",
        tech_lead_bot_open_id: str = "",
        impersonation_token_service: ImpersonationTokenService | None = None,
        project_id: str = "",
        command_text: str = "",
        trace_id: str = "",
        chat_id: str | None = None,
        recent_conversation: list[dict[str, Any]] | None = None,
        role_name: str = "tech_lead",
        timeout_seconds: int = 120,
        thread_update_fn: ThreadUpdateFn | None = None,
        role_executor_provider: RoleExecutorProvider | None = None,
        workflow_service: WorkflowService | None = None,
        speckit_script_service: SpeckitScriptService | None = None,
        code_write_service: CodeWriteService | None = None,
        pre_push_inspector: PrePushInspector | None = None,
        git_ops_service: GitOpsService | None = None,
        pull_request_service: PullRequestService | None = None,
        ci_watch_service: CIWatchService | None = None,
        agent_notes_service: AgentNotesService | None = None,
        hook_bus: "HookBus | None" = None,
        cancel_token: "CancelToken | None" = None,
        task_handle: TaskHandle | None = None,
        artifact_store: ArtifactStore | None = None,
        root_trace_id: str | None = None,
        worktree_manager: WorktreeManager | None = None,
        task_graph: TaskGraph | None = None,
        **_kwargs: Any,
    ) -> None:
        self._progress_sync = progress_sync_service
        self._sprint_state = sprint_state_service
        self._audit = audit_service
        self._llm_agent = llm_agent_adapter
        self._role_registry = role_registry
        self._pending = pending_action_service
        self._feishu_client = feishu_client
        self._app_agent_open_id = application_agent_open_id
        self._app_agent_group_chat_id = application_agent_group_chat_id
        self._app_delegate_url = (application_agent_delegate_url or "").strip() or None
        self._app_agent_label = (application_agent_display_name or "").strip() or "Application delegate"
        self._tech_lead_bot_open_id = (tech_lead_bot_open_id or "").strip()
        self._impersonation_token_service = impersonation_token_service
        self.project_id = project_id
        self.command_text = command_text
        self.trace_id = trace_id
        self.chat_id = chat_id
        self.recent_conversation = recent_conversation or []
        self.role_name = role_name
        self.timeout_seconds = timeout_seconds
        self._thread_update = thread_update_fn
        self._role_executor_provider = role_executor_provider
        self._workflow = workflow_service
        self._speckit_scripts = speckit_script_service
        self._workflow_agent_name = "tech_lead"
        self._code_write = code_write_service
        self._pre_push_inspector = pre_push_inspector
        self._git_ops = git_ops_service
        self._pull_request = pull_request_service
        self._ci_watch = ci_watch_service
        self._agent_notes = agent_notes_service
        self._hook_bus = hook_bus
        self._cancel_token = cancel_token
        self._task_handle = task_handle
        self._artifact_store = artifact_store
        # Root trace id for the A-3 team layout. When unset we
        # default to ``trace_id`` — the TL is usually the top-most
        # session so parent == root. A nested TL (future: teams of
        # TLs) would override this with its own parent session id
        # so every child's artifacts land under the same team dir.
        self._root_trace_id = root_trace_id or trace_id or ""
        # Story 004.5 — B-3 worktree isolation + B-1 claim lease
        # wiring. Both are optional: when the runtime can't produce
        # them (e.g. no repo_root resolvable, no sprint_state
        # service configured) the dispatch path degrades to pre-wiring
        # behaviour (``working_dir == repo_root``; no claim taken).
        # Dispatch-scoped registry of worktree handles keyed by
        # ``child_trace_id``. Populated before the sub-session
        # spawns, popped + released in every ``_dispatch_role_agent``
        # exit path. This is how the ``finally`` branch reaches
        # back to the provider's acquire call without changing
        # the provider's public signature beyond the ``working_dir``
        # override.
        self._worktree_manager = worktree_manager
        self._active_worktrees: dict[str, WorktreeHandle] = {}
        # Same idea for TaskGraph claims: empty dict when no task_id
        # was supplied (which is every dispatch today), or a mapping
        # ``child_trace_id → task_id`` so the ``finally`` path knows
        # whether to call ``complete``/``release``.
        self._task_graph = task_graph
        self._active_task_claims: dict[str, str] = {}
        # Cache for ``_provider_accepts_working_dir`` — computed once
        # per TL instance so we don't re-introspect on every dispatch.
        # Kept as Optional so we can lazily populate (the provider may
        # not be set yet at __init__ time).
        self._provider_accepts_working_dir_cache: bool | None = None
        # Drop write_project_code* from TL's tool surface so the
        # developer role owns actual code authoring. Reads + git
        # gatekeeper tools stay.
        self._code_write_tool_allow: "frozenset[str] | None" = TECH_LEAD_CODE_WRITE_ALLOW
        self._start_time = time.monotonic()
        self.last_table_name: str | None = None

    def _provider_accepts_working_dir(self) -> bool:
        """Story 004.5 H3 — does ``self._role_executor_provider``
        accept a ``working_dir`` keyword argument?

        We answer once and cache the result. The provider's signature
        is fixed for the lifetime of the TL (the runtime builds one
        provider per session), so caching is safe. Providers that
        accept ``**kwargs`` count as accepting ``working_dir``.

        Called from the dispatch hot path; failure modes downgrade to
        ``False`` so an introspection error never blocks a dispatch.
        """
        if self._provider_accepts_working_dir_cache is not None:
            return self._provider_accepts_working_dir_cache
        provider = self._role_executor_provider
        if provider is None:
            self._provider_accepts_working_dir_cache = False
            return False
        try:
            sig = inspect.signature(provider)
        except (TypeError, ValueError):
            # Builtin / C-level callable that can't be introspected.
            # Safer default: don't pass the kwarg.
            self._provider_accepts_working_dir_cache = False
            return False
        params = sig.parameters
        accepts = "working_dir" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        self._provider_accepts_working_dir_cache = accepts
        return accepts

    def tool_specs(self) -> list[AgentToolSpec]:
        # append_agent_note is only surfaced when the service is wired.
        # Keeps the tool out of the LLM's menu in test/dev builds that
        # don't configure per-project memory — otherwise the LLM sees
        # the tool, calls it, and we return a generic "disabled" error
        # which is worse UX than the tool simply not existing.
        specs = list(TECH_LEAD_V2_TOOL_SPECS)
        if self._agent_notes is None or not self._agent_notes.enabled:
            specs = [s for s in specs if s.name != "append_agent_note"]
        # resume_last_dispatch needs the per-thread event log to locate
        # the prior dispatch_role_agent call. In harnesses / legacy
        # tests that don't wire a TaskHandle, hide the tool rather than
        # surface it and then fail with NO_TASK_HANDLE at call time.
        if self._task_handle is None:
            specs = [s for s in specs if s.name != "resume_last_dispatch"]
        return (
            specs
            + self.workflow_tool_specs()
            + self.code_write_tool_specs()
            + self.speckit_script_tool_specs()
        )

    def _emit_code_write_update(self, line: str) -> None:
        self._fire_thread_update(line)

    async def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | list[Any] | str:
        if tool_name == "read_sprint_status":
            parsed = SprintArgs.model_validate(arguments)
            return read_sprint_status(self._sprint_state, parsed.sprint)
        if tool_name == "advance_sprint_state":
            parsed = AdvanceSprintStateArgs.model_validate(arguments)
            return self._advance_sprint_state(parsed)
        if tool_name == "dispatch_role_agent":
            parsed = DispatchRoleAgentArgs.model_validate(arguments)
            return await self._dispatch_role_agent(parsed)
        if tool_name == "resume_last_dispatch":
            parsed = ResumeLastDispatchArgs.model_validate(arguments)
            return await self._resume_last_dispatch(parsed)
        if tool_name == "request_confirmation":
            parsed = RequestConfirmationArgs.model_validate(arguments)
            return self._request_confirmation(parsed)
        if tool_name == "delegate_to_application_agent":
            parsed = DelegateToApplicationAgentArgs.model_validate(arguments)
            return await self._delegate_to_application_agent(parsed)
        if tool_name == "append_agent_note":
            parsed = AppendAgentNoteArgs.model_validate(arguments)
            return self._append_agent_note(parsed)
        workflow_result = await self.handle_workflow_tool(tool_name, arguments)
        if workflow_result is not None:
            return workflow_result
        speckit_result = await self.handle_speckit_script_tool(tool_name, arguments)
        if speckit_result is not None:
            return speckit_result
        code_result = await self.handle_code_write_tool(tool_name, arguments)
        if code_result is not None:
            return code_result
        raise RuntimeError(f"Unsupported tool: {tool_name}")

    # ------------------------------------------------------------------
    # append_agent_note
    # ------------------------------------------------------------------

    def _append_agent_note(self, args: AppendAgentNoteArgs) -> dict[str, Any]:
        """Persist a short project note for cross-session memory.

        Error shape is deliberately LLM-friendly: a stable ``error`` code
        (see ``AgentNoteError.code``) so the model can branch ("oversize
        → split the note") without having to parse human-readable
        messages. The thread update gives the user real-time visibility
        into what the agent is choosing to remember.
        """
        if self._agent_notes is None:
            return {
                "stored": False,
                "error": "AGENT_NOTE_DISABLED",
                "detail": "agent notes service is not configured",
            }
        try:
            entry = self._agent_notes.append(
                role=self.role_name,
                note=args.note,
                trace_id=self.trace_id or None,
            )
        except AgentNoteError as exc:
            return {
                "stored": False,
                "error": exc.code,
                "detail": str(exc),
            }

        self._fire_thread_update(
            f"📝 记录项目笔记: {entry.note[:60]}"
            + ("…" if len(entry.note) > 60 else "")
        )
        return {
            "stored": True,
            "project_id": entry.project_id,
            "role": entry.role,
            "timestamp": entry.timestamp_iso,
            "note_chars": len(entry.note),
            "notes_path": str(self._agent_notes.notes_path),
        }

    # ------------------------------------------------------------------
    # request_confirmation
    # ------------------------------------------------------------------

    def _request_confirmation(self, args: RequestConfirmationArgs) -> dict[str, Any]:
        if self._pending is None:
            return {
                "stored": False,
                "error": "PendingActionService not configured",
            }

        from uuid import uuid4

        pending_trace = f"pending-{uuid4().hex[:12]}"
        action = PendingAction(
            trace_id=pending_trace,
            chat_id=self.chat_id or "",
            role_name=self.role_name,
            action_type=args.action_type,
            action_args=dict(args.action_args),
            confirmation_message_id=None,
        )
        self._pending.save(action, root_trace_id=self.trace_id or None)
        return {
            "stored": True,
            "pending_trace_id": pending_trace,
            "action_type": args.action_type,
            "summary": args.summary,
            "instruction": "请将以下确认信息展示给用户，并告知用户回复「确认」执行或「取消」放弃：\n\n" + args.summary,
        }

    # ------------------------------------------------------------------
    # dispatch_role_agent
    # ------------------------------------------------------------------

    def _remaining_seconds(self, reserve: float = 30.0) -> int:
        """Return seconds left in the overall budget minus a reserve for the final LLM turn."""
        elapsed = time.monotonic() - self._start_time
        remaining = max(int(self.timeout_seconds - elapsed - reserve), 15)
        return remaining

    def _fire_thread_update(self, text: str) -> None:
        if not self._thread_update:
            return
        try:
            self._thread_update(text)
        except Exception:
            logger.warning("thread update callback failed", exc_info=True)

    @staticmethod
    def _truncate_task(task: str, limit: int = 60) -> str:
        if len(task) <= limit:
            return task
        return task[:limit] + "…"

    # Tools whose NAME alone is enough signal for a user watching the
    # Feishu thread — we surface them as compact phase markers. Reads /
    # lists / describes are all collapsed into a single tool-name line
    # per session (not per call), and writes / commits / pushes emit
    # their name only (no args, no duration). The dispatch post-
    # processor emits the real summary via ``TOOL_CATEGORIES`` once the
    # sub-agent is done — see ``_summarize_tool_calls``.
    _IMPACTFUL_TOOLS: frozenset[str] = frozenset(
        {
            "write_project_code",
            "write_project_code_batch",
            "git_commit",
            "git_push",
            "run_pre_push_inspection",
            "create_pull_request",
            "write_role_artifact",
            "advance_sprint_state",
            "request_confirmation",
        }
    )

    def _build_tool_call_observer(
        self, role_name: str
    ) -> tuple[
        Callable[[str, dict[str, Any], Any, int], None], "dict[str, int]"
    ]:
        """Return ``(observer, counter)``.

        Streaming-to-Feishu policy is **summary first, detail only on
        error**. The user wanted "just the tech-lead's one-paragraph
        transcript of what each role did", not a live tool feed. So the
        observer:

        - **Emits nothing for successful tool calls.** Every success is
          folded into the shared counter, which the dispatch post-
          processor renders into one summary line
          (``_summarize_tool_calls``) attached to the "X ✅ 已完成"
          banner.
        - **Surfaces every error** immediately so the user sees failures
          in context — compact form: tool name, truncated args, duration.

        The counter is returned so ``_dispatch_role_agent`` can reach
        into it when building the completion summary.
        """

        session_tool_counts: dict[str, int] = {}

        def _observer(tool_name: str, args: dict[str, Any], result: Any, duration_ms: int) -> None:
            session_tool_counts[tool_name] = (
                session_tool_counts.get(tool_name, 0) + 1
            )

            is_error = isinstance(result, dict) and (
                result.get("error") is not None
                or str(result.get("success")).lower() == "false"
            )
            if not is_error:
                return

            try:
                args_preview = json.dumps(args, ensure_ascii=False, default=str)
            except Exception:
                args_preview = str(args)
            if len(args_preview) > 80:
                args_preview = args_preview[:77] + "…"

            # Swallow ⚠️ for LLM-self-correctable fast-fails (missing
            # pydantic args, wrong tool / workflow id guesses). Still
            # record in the agent log so the error is discoverable from
            # ``feishu-agent.log`` without polluting Feishu.
            if _is_noisy_tool_error(result):
                logger.info(
                    "suppressed ⚠️ noise role=%s tool=%s error=%s duration=%dms",
                    role_name,
                    tool_name,
                    result.get("error") if isinstance(result, dict) else result,
                    duration_ms,
                )
                return

            self._fire_thread_update(
                f"⚠️ {role_name} → {tool_name}{args_preview} ({duration_ms}ms)"
            )

        return _observer, session_tool_counts

    # Tools that write to disk under a role-owned root. We map them to
    # the ``FileTouch.kind`` vocabulary from A-3 so the artifact's
    # ``files_touched`` list is self-describing without the reader
    # having to correlate tool names against bundle metadata.
    _FILE_WRITE_TOOLS: dict[str, str] = {
        "write_project_code": "write",
        "write_project_code_batch": "batch_write",
        "write_role_artifact": "write",
        "write_file": "write",
        "delete_project_code": "delete",
    }

    def _build_artifact_recorder(
        self,
    ) -> tuple[
        Callable[[str, dict[str, Any], Any, int], None],
        list[ToolCallRecord],
        list[FileTouch],
    ]:
        """Return ``(observer, tool_records, files_touched)``.

        Chained alongside the Feishu-summary observer from
        :meth:`_build_tool_call_observer` — both fire for every
        tool call. This one has a single job: populate the A-3
        envelope. Truncation happens *at record time* so a rogue
        1MB tool result can't balloon the artifact payload later.

        ``files_touched`` is a best-effort stub: we capture
        ``path`` + ``kind`` + ``bytes_written`` by inspecting the
        tool name and the shape of the result dict. A later spec
        (OQ-004-5) will swap this for full diff capture driven by
        ``code_write_service`` hooks. Until then, callers can use
        the stub to scan for "did a role touch X?" without running
        a full replay.
        """

        records: list[ToolCallRecord] = []
        files: list[FileTouch] = []

        def _record(
            tool_name: str,
            args: dict[str, Any],
            result: Any,
            duration_ms: int,
        ) -> None:
            # ``started_at`` is reverse-derived from ``duration_ms``
            # so the record's timestamp matches what the caller
            # observed, not when we happened to project it. Matters
            # for replay ordering across parallel dispatches.
            now_ms = int(time.time() * 1000)
            started_at = max(now_ms - duration_ms, 0)
            is_error = isinstance(result, dict) and (
                result.get("error") is not None
                or str(result.get("success")).lower() == "false"
            )
            records.append(
                ToolCallRecord(
                    tool_name=tool_name,
                    arguments_preview=truncate_preview(args, ARGS_PREVIEW_MAX),
                    result_preview=truncate_preview(result, RESULT_PREVIEW_MAX),
                    duration_ms=duration_ms,
                    is_error=is_error,
                    started_at=started_at,
                )
            )

            kind = self._FILE_WRITE_TOOLS.get(tool_name)
            if kind is None or is_error:
                # Non-write tools and failed writes don't produce
                # a touch entry — the latter because a half-committed
                # write doesn't leave a file behind on our write
                # paths; code_write_service either succeeds fully
                # or reverts.
                return
            for touch in _extract_file_touches(tool_name, kind, args, result):
                files.append(touch)

        return _record, records, files

    # Map of tool name → short Chinese label used when rendering the
    # completion summary. Tools not in this map are ignored in the
    # summary (no one cares that read_sprint_status was called once).
    _TOOL_SUMMARY_LABELS: dict[str, str] = {
        "write_project_code": "写文件",
        "write_project_code_batch": "批量写",
        "git_commit": "commit",
        "git_push": "push",
        "run_pre_push_inspection": "inspection",
        "create_pull_request": "开 PR",
        "write_role_artifact": "产出 artifact",
        "read_project_code": "读源码",
        "list_project_paths": "列目录",
        "git_sync_remote": "同步 remote",
    }

    @classmethod
    def _summarize_tool_calls(cls, counts: dict[str, int]) -> str:
        """Render a one-liner of what the sub-agent actually did.

        Example: "读源码×7, 列目录×2, 批量写×1, commit×1, 产出 artifact×1".
        Returns an empty string if nothing interesting happened — caller
        should skip the summary line in that case rather than print an
        awkward trailing em-dash.
        """
        parts: list[str] = []
        for tool, label in cls._TOOL_SUMMARY_LABELS.items():
            n = counts.get(tool, 0)
            if n <= 0:
                continue
            parts.append(f"{label}×{n}")
        return ", ".join(parts)

    async def _dispatch_role_agent(self, args: DispatchRoleAgentArgs) -> dict[str, Any]:
        start = time.monotonic()
        # Wall-clock start in unix ms — used for the A-3 artifact's
        # ``started_at`` field. ``time.monotonic()`` can't be used
        # here because it isn't tied to wall clock and the artifact
        # is meant to be readable / diffable across processes.
        start_ms = int(time.time() * 1000)
        try:
            role = self._role_registry.get_role(args.role_name)
        except RoleNotFoundError:
            # UNKNOWN_ROLE is a pre-dispatch guard: no child session
            # was ever attempted, so there's no artifact to write.
            # We fail fast and leave the A-3 envelope untouched.
            return {
                "role_name": args.role_name,
                "task": args.task,
                "success": False,
                "output": "",
                "error": f"UNKNOWN_ROLE: {args.role_name}",
                "latency_ms": int((time.monotonic() - start) * 1000),
            }

        sub_timeout = self._remaining_seconds(reserve=30)

        # Refuse to dispatch when the remaining budget is too small
        # to produce a meaningful result. Better to tell the user
        # "out of time, re-dispatch me" than to burn a turn handing
        # a sub-agent 40 seconds it can't use.
        if sub_timeout < MIN_SUB_AGENT_TIMEOUT_SECONDS:
            self._fire_thread_update(
                f"⏳ 跳过委派 {args.role_name}：剩余预算仅 {sub_timeout}s，"
                f"低于最小 {MIN_SUB_AGENT_TIMEOUT_SECONDS}s；"
                f"请重新 @ 我继续推进。"
            )
            # Same rationale as UNKNOWN_ROLE: the sub-agent was
            # never spawned, so the artifact store stays quiet.
            return {
                "role_name": args.role_name,
                "task": args.task,
                "success": False,
                "output": "",
                "error": "OUT_OF_BUDGET",
                "message": (
                    f"Parent tech-lead budget has {sub_timeout}s left, "
                    f"which is below the {MIN_SUB_AGENT_TIMEOUT_SECONDS}s "
                    f"minimum for a sub-agent. Stop the session and ask "
                    f"the user to re-dispatch; DO NOT retry this call."
                ),
                "latency_ms": int((time.monotonic() - start) * 1000),
            }

        task_preview = self._truncate_task(args.task)
        self._fire_thread_update(f"⏳ 已委派 {args.role_name} 执行：{task_preview}")

        prompt = role.system_prompt
        if args.acceptance_criteria:
            prompt += f"\n\nAcceptance criteria: {args.acceptance_criteria}"
        if args.workflow_id:
            # Pin the sub-agent to a specific BMAD / speckit procedure.
            # The role's mandatory_workflow already tells it to load a
            # bmad:* methodology first, but the TL often knows a more
            # specific id than the default (e.g. bmad:correct-course
            # for a bug_fixer dispatch vs. the role's default).
            prompt += (
                f"\n\n<dispatch_workflow>\n"
                f"Tech lead pins this session to workflow_id="
                f"'{args.workflow_id}'.\n"
                f"Your FIRST tool call MUST be "
                f"read_workflow_instruction('{args.workflow_id}') — "
                f"follow the rubric it returns for the rest of this "
                f"session. Do not invent a different rubric from memory.\n"
                f"</dispatch_workflow>"
            )

        # Child trace id — lets audit/lineage/thread-update distinguish
        # this dispatch from other sub-agent spawns in the same parent
        # session. Uses ``uuid4`` (no secret entropy needed) so collisions
        # are vanishingly unlikely even under heavy parallel dispatch.
        child_trace_id = f"{self.trace_id or 'notrace'}-{uuid4().hex[:12]}"

        # Story 004.5 — take a TaskGraph claim lease FIRST (AC-8).
        # Done before the worktree acquire so a claim conflict aborts
        # dispatch without leaving a stale worktree on disk. An empty
        # ``task_id`` (the default) skips the entire claim path; a
        # non-empty id that fails to claim (conflict, not-found) is
        # reported in the result payload but does NOT abort dispatch —
        # the LLM is free to retry with a different id or proceed
        # without a claim. Ownership errors are impossible here because
        # ``child_trace_id`` is freshly generated.
        claim_warning: str | None = None
        claim_skip_reason: str | None = None
        if (
            args.task_id
            and self._task_graph is not None
        ):
            try:
                # AC-8 verbatim: ``ttl_seconds=sub_timeout``. No extra
                # floor — the earlier ``if sub_timeout <
                # MIN_SUB_AGENT_TIMEOUT_SECONDS: return`` branch (see
                # the ``OUT_OF_BUDGET`` path above) guarantees we only
                # reach this line with ``sub_timeout >=
                # MIN_SUB_AGENT_TIMEOUT_SECONDS`` (currently 120s),
                # which is already comfortably above any reasonable
                # lease floor.
                self._task_graph.claim(
                    args.task_id,
                    child_trace_id,
                    ttl_seconds=int(sub_timeout),
                )
                self._active_task_claims[child_trace_id] = args.task_id
            except ClaimConflictError as exc:
                claim_skip_reason = "conflict"
                claim_warning = (
                    f"TASK_CLAIM_SKIPPED: task_id={args.task_id!r} is "
                    f"not claimable ({exc}). Dispatch proceeded without "
                    f"a claim; the task's sprint-status entry is "
                    f"unchanged."
                )
                logger.warning("%s", claim_warning)
            except TaskNotFoundError:
                claim_skip_reason = "not_found"
                claim_warning = (
                    f"TASK_CLAIM_SKIPPED: task_id={args.task_id!r} is "
                    f"not present in sprint-status.yaml tasks block. "
                    f"Dispatch proceeded without a claim."
                )
                logger.warning("%s", claim_warning)
            except Exception as exc:  # defensive — never break dispatch
                claim_skip_reason = "unexpected_error"
                claim_warning = (
                    f"TASK_CLAIM_SKIPPED: unexpected error acquiring "
                    f"task_id={args.task_id!r}: {exc}"
                )
                logger.warning("%s", claim_warning, exc_info=True)
            # Story 004.5 M3 — when the claim path is skipped, emit an
            # explicit ``claim.skipped`` audit event so operators who
            # grep ``claim.*`` in the jsonl stream see failed-claim
            # dispatches just as clearly as successful ones. The
            # ``claim_warning`` already lives in the dispatch result
            # payload; duplicating the signal to the audit stream
            # closes the observability gap flagged in the runbook.
            if claim_skip_reason is not None:
                try:
                    self._audit.record(
                        "claim.skipped",
                        {
                            "child_trace_id": child_trace_id,
                            "role_name": args.role_name,
                            "task_id": args.task_id,
                            "reason": claim_skip_reason,
                        },
                    )
                except Exception:
                    logger.exception(
                        "audit emit failed for claim.skipped"
                    )

        # Story 004.5 — acquire the worktree BEFORE building the
        # sub-executor so the provider sees ``working_dir=handle.path``
        # when it constructs the child's BundleContext. Gate on both
        # ``role.needs_worktree`` (frontmatter opt-in: developer /
        # bug_fixer) AND a wired manager; roles that don't opt in
        # keep their pre-004.5 ``working_dir == repo_root`` behaviour.
        worktree_handle: WorktreeHandle | None = None
        if (
            self._worktree_manager is not None
            and getattr(role, "needs_worktree", False)
        ):
            try:
                worktree_handle = self._worktree_manager.acquire(
                    child_trace_id=child_trace_id,
                    base_branch=role.worktree_base_branch,
                )
                self._active_worktrees[child_trace_id] = worktree_handle
                try:
                    self._audit.record(
                        "worktree.acquire",
                        {
                            "child_trace_id": child_trace_id,
                            "role_name": args.role_name,
                            "path": str(worktree_handle.path),
                            "branch": worktree_handle.branch,
                            "fallback": worktree_handle.is_fallback,
                        },
                    )
                except Exception:
                    # Audit failure must not crash dispatch. The
                    # handle is still live.
                    logger.exception(
                        "audit emit failed for worktree.acquire"
                    )
            except Exception:
                # ``WorktreeManager.acquire`` already downgrades its
                # own errors to fallback handles, so we only land
                # here if something catastrophic happened (e.g. an
                # injected manager with a bug). Log and proceed
                # without a handle.
                logger.exception(
                    "worktree_manager.acquire raised unexpectedly; "
                    "dispatch will run in the main working copy"
                )
                worktree_handle = None

        sub_executor: AgentToolExecutor | None = None
        if self._role_executor_provider is not None:
            # Story 004.5 H3 — decide whether the provider accepts the
            # ``working_dir`` override by introspection, not by catching
            # TypeError. A broad ``except TypeError`` would swallow any
            # unrelated type error inside the provider body and silently
            # demote a buggy dispatch to "no worktree isolation" with
            # nothing in the logs. ``_provider_accepts_working_dir``
            # answers the question once (cached on the bound provider)
            # and we pass the kwarg only when it's truly accepted.
            provider_kwargs: dict[str, Any] = {}
            if (
                worktree_handle is not None
                and not worktree_handle.is_fallback
                and self._provider_accepts_working_dir()
            ):
                # Only override the provider's default ``working_dir``
                # when we have a non-fallback handle; a fallback handle
                # already points at ``repo_root`` so the override would
                # be a no-op but would signal an intent the provider
                # can't honour.
                provider_kwargs["working_dir"] = worktree_handle.path
            try:
                sub_executor = self._role_executor_provider(
                    args.role_name, role, **provider_kwargs
                )
            except Exception:
                logger.exception(
                    "role_executor_provider failed for %s",
                    args.role_name,
                )
                sub_executor = None
        if self._hook_bus is not None:
            await self._hook_bus.afire(
                "on_sub_agent_spawn",
                {
                    "parent_trace_id": self.trace_id,
                    "child_trace_id": child_trace_id,
                    "role": args.role_name,
                },
            )

        # Build the observer up-front so the ``spawn_sub_agent`` (non-tool)
        # branch below can still share the same counter object — even
        # though that branch never emits tool calls, keeping one code path
        # for the "what did X actually do" summary avoids a None check
        # later.
        tool_observer, tool_counter = self._build_tool_call_observer(args.role_name)

        # A-3 envelope recorder — chains alongside the Feishu-summary
        # observer so every tool call is captured in the artifact
        # exactly once. ``specs_by_name`` is frozen here because the
        # sub-executor's tool surface is composed once per dispatch
        # (bundles re-resolve on the next call); capturing it now
        # keeps risk-scoring stable even if the registry hot-reloads
        # mid-run.
        (
            artifact_recorder,
            artifact_tool_records,
            artifact_files_touched,
        ) = self._build_artifact_recorder()
        specs_by_name: dict[str, AgentToolSpec] = {}
        if sub_executor is not None:
            try:
                specs_by_name = {s.name: s for s in sub_executor.tool_specs()}
            except Exception:
                # A rogue executor that raises on tool_specs() is
                # still allowed to run; we just fall back to the
                # conservative "everything counts as world" default
                # inside compute_risk_score.
                specs_by_name = {}

        def _combined_observer(
            tool_name: str,
            call_args: dict[str, Any],
            call_result: Any,
            call_duration: int,
        ) -> None:
            tool_observer(tool_name, call_args, call_result, call_duration)
            try:
                artifact_recorder(tool_name, call_args, call_result, call_duration)
            except Exception:
                # The recorder is best-effort; never let an A-3
                # projection bug crash the dispatch hot path.
                logger.exception("artifact_recorder raised; continuing")

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
                    timeout=sub_timeout,
                    on_tool_call=_combined_observer,
                    hook_bus=self._hook_bus,
                    cancel_token=self._cancel_token,
                    trace_id=child_trace_id,
                )
            else:
                result = await self._llm_agent.spawn_sub_agent(
                    role_name=args.role_name,
                    task=args.task,
                    system_prompt=prompt,
                    tools_allow=role.tool_allow_list or None,
                    model=role.model,
                    timeout=sub_timeout,
                )
        except TimeoutError:
            elapsed_s = (time.monotonic() - start)
            self._fire_thread_update(
                f"❌ {args.role_name} 执行超时（{elapsed_s:.0f}s，预算 {sub_timeout}s）"
            )
            await self._fire_sub_agent_end(
                child_trace_id, args.role_name, ok=False, stop_reason="timeout"
            )
            # Story 004.5 — release worktree + task claim on the
            # timeout path. ``success=False`` keeps the worktree on
            # disk for post-mortem (B-3 ``keep_on_failure=True``)
            # and flips the task back to ``pending``.
            wt_fallback = self._release_worktree_for(
                child_trace_id, success=False
            )
            self._release_task_claim_for(child_trace_id, success=False)
            art_info = self._finalize_artifact(
                args=args,
                role=role,
                child_trace_id=child_trace_id,
                started_at_ms=start_ms,
                tool_records=artifact_tool_records,
                files_touched=artifact_files_touched,
                specs_by_name=specs_by_name,
                stop_reason="timeout",
                success=False,
                output_text="",
                error_message="AGENT_TIMEOUT",
                token_usage={},
                worktree_fallback=wt_fallback,
            )
            return {
                "role_name": args.role_name,
                "task": args.task,
                "success": False,
                "output": "",
                "error": "AGENT_TIMEOUT",
                "latency_ms": int((time.monotonic() - start) * 1000),
                **({"claim_warning": claim_warning} if claim_warning else {}),
                **art_info,
            }
        except Exception as exc:
            self._fire_thread_update(f"❌ {args.role_name} 执行异常：{str(exc)[:80]}")
            await self._fire_sub_agent_end(
                child_trace_id, args.role_name, ok=False, stop_reason="error"
            )
            wt_fallback = self._release_worktree_for(
                child_trace_id, success=False
            )
            self._release_task_claim_for(child_trace_id, success=False)
            art_info = self._finalize_artifact(
                args=args,
                role=role,
                child_trace_id=child_trace_id,
                started_at_ms=start_ms,
                tool_records=artifact_tool_records,
                files_touched=artifact_files_touched,
                specs_by_name=specs_by_name,
                stop_reason="error",
                success=False,
                output_text="",
                error_message=str(exc),
                token_usage={},
                worktree_fallback=wt_fallback,
            )
            return {
                "role_name": args.role_name,
                "task": args.task,
                "success": False,
                "output": "",
                "error": str(exc),
                "latency_ms": int((time.monotonic() - start) * 1000),
                **({"claim_warning": claim_warning} if claim_warning else {}),
                **art_info,
            }

        latency_ms = result.latency_ms or int((time.monotonic() - start) * 1000)
        latency_s = latency_ms / 1000
        # One-liner summary of what the sub-agent actually did (counts by
        # tool category). Keeps the user informed without streaming every
        # individual tool call.
        summary_line = self._summarize_tool_calls(tool_counter)
        if result.success:
            base = f"{args.role_name} ✅ 已完成（{latency_s:.1f}s）"
            self._fire_thread_update(
                f"{base}\n行为概要：{summary_line}" if summary_line else base
            )
        else:
            reason = (result.error_message or "")[:80]
            base = f"{args.role_name} ❌ 失败（{latency_s:.1f}s）"
            lines = [base]
            if reason:
                lines.append(f"原因：{reason}")
            if summary_line:
                lines.append(f"行为概要：{summary_line}")
            self._fire_thread_update("\n".join(lines))

        await self._fire_sub_agent_end(
            child_trace_id,
            args.role_name,
            ok=bool(result.success),
            stop_reason=result.stop_reason,
        )

        # Story 004.5 — normal exit path. Release worktree + claim
        # BEFORE artifact finalisation so the artifact records the
        # definitive ``worktree_fallback`` value; this also guarantees
        # that a concurrent acquire for the same ``child_trace_id``
        # (if the caller retries idempotently) gets a fresh handle.
        wt_fallback = self._release_worktree_for(
            child_trace_id, success=bool(result.success)
        )
        self._release_task_claim_for(
            child_trace_id, success=bool(result.success)
        )

        art_info = self._finalize_artifact(
            args=args,
            role=role,
            child_trace_id=child_trace_id,
            started_at_ms=start_ms,
            tool_records=artifact_tool_records,
            files_touched=artifact_files_touched,
            specs_by_name=specs_by_name,
            stop_reason=result.stop_reason
            or ("complete" if result.success else "error"),
            success=bool(result.success),
            output_text=result.content or "",
            error_message=result.error_message,
            token_usage=dict(result.token_usage or {}),
            worktree_fallback=wt_fallback,
        )

        return {
            "role_name": args.role_name,
            "task": args.task,
            "success": result.success,
            "output": result.content,
            "error": result.error_message,
            "latency_ms": latency_ms,
            **({"claim_warning": claim_warning} if claim_warning else {}),
            **art_info,
        }

    def _release_worktree_for(
        self, child_trace_id: str, *, success: bool
    ) -> bool:
        """Story 004.5 — pop + release a worktree handle acquired
        earlier in this dispatch.

        Returns ``handle.is_fallback`` so the caller can plumb it into
        :meth:`_finalize_artifact`. Returns ``False`` when no handle
        is tracked for this dispatch (role doesn't need a worktree).
        Never raises: audit emission + git removal failures are
        downgraded to warnings. Called from every exit path of
        :meth:`_dispatch_role_agent` so that even the most exotic
        failure (LLM timeout, cancellation, ZeroDivisionError in the
        observer) still cleans up the worktree + emits the
        ``worktree.release`` event.
        """
        handle = self._active_worktrees.pop(child_trace_id, None)
        if handle is None:
            return False
        if self._worktree_manager is not None:
            try:
                self._worktree_manager.release(
                    handle, keep_on_failure=True, success=success
                )
            except Exception:
                logger.exception(
                    "worktree_manager.release raised for %s",
                    child_trace_id,
                )
        # Story 004.5 Q4 answer — emit ``worktree.release`` even for
        # fallback handles so operators can grep a complete
        # acquire/release pair per dispatch. The audit payload
        # includes ``fallback`` so downstream filters can cheaply
        # drop them if they want only "real" releases.
        try:
            self._audit.record(
                "worktree.release",
                {
                    "child_trace_id": child_trace_id,
                    "path": str(handle.path),
                    "branch": handle.branch,
                    "fallback": handle.is_fallback,
                    "success": success,
                    "kept_on_failure": (not success),
                },
            )
        except Exception:
            logger.exception("audit emit failed for worktree.release")
        return handle.is_fallback

    def _release_task_claim_for(
        self, child_trace_id: str, *, success: bool
    ) -> None:
        """Story 004.5 / AC-8 — mirror of :meth:`_release_worktree_for`
        but for TaskGraph claims. No-op when the dispatch never took a
        claim (empty ``task_id``). Always pops the entry on exit so a
        retry with the same id can re-claim cleanly."""
        task_id = self._active_task_claims.pop(child_trace_id, None)
        if task_id is None or self._task_graph is None:
            return
        try:
            if success:
                self._task_graph.complete(task_id, child_trace_id)
            else:
                self._task_graph.release(task_id, child_trace_id)
        except ClaimOwnershipError:
            # Lease already expired and a different trace (or the
            # housekeeper) released it. That's fine — the runtime
            # invariant is "we never hold a lease past dispatch
            # exit"; whoever reclaimed it gets ownership. No audit
            # emit because ``TaskGraph`` already emits ``claim.expired``
            # in its release_expired path.
            logger.info(
                "task_graph: claim for %s no longer owned by %s on "
                "release (lease likely expired)",
                task_id,
                child_trace_id,
            )
        except Exception:
            logger.exception(
                "task_graph.%s failed for %s/%s",
                "complete" if success else "release",
                task_id,
                child_trace_id,
            )

    def _finalize_artifact(
        self,
        *,
        args: DispatchRoleAgentArgs,
        role: RoleDefinition,
        child_trace_id: str,
        started_at_ms: int,
        tool_records: list[ToolCallRecord],
        files_touched: list[FileTouch],
        specs_by_name: dict[str, AgentToolSpec],
        stop_reason: str,
        success: bool,
        output_text: str,
        error_message: str | None,
        token_usage: dict[str, int],
        worktree_fallback: bool = False,
    ) -> dict[str, Any]:
        """Assemble, persist, and announce the A-3 role artifact.

        Called from every post-dispatch exit path (success, failure,
        timeout, exception). Always returns a plain dict with the
        two fields that need to reach the LLM in the dispatch
        result: ``artifact_id`` and ``artifact_path``. Never
        raises — every error is swallowed to a warning so an
        artifact-layer bug can't regress dispatch correctness.
        """
        # Feature-flag short-circuit. Constructor wiring only passes
        # an ``artifact_store`` when ``settings.artifact_store_enabled``
        # is true; a None here means "operator opted out", not
        # "store misconfigured". Return empty so the caller's dict
        # spread is a no-op.
        if self._artifact_store is None:
            return {}

        completed_at = int(time.time() * 1000)
        concurrency_group = (
            getattr(role, "concurrency_group", None) or args.role_name
        )
        artifact = RoleArtifact(
            artifact_id=child_trace_id,
            parent_trace_id=self.trace_id or "",
            # ``root_trace_id`` is the team-dir key. Defaults to
            # ``trace_id`` when no explicit root was injected (normal
            # TL-is-the-root case). Leaving it empty would put the
            # artifact under ``teams//artifacts/`` which ArtifactStore
            # tolerates but isn't meaningful; we substitute
            # ``"notrace"`` rather than hide the dispatch.
            root_trace_id=self._root_trace_id or self.trace_id or "notrace",
            role_name=args.role_name,
            task=args.task,
            acceptance_criteria=args.acceptance_criteria or "",
            started_at=started_at_ms,
            completed_at=completed_at,
            duration_ms=max(completed_at - started_at_ms, 0),
            success=success,
            stop_reason=stop_reason,
            tool_calls=list(tool_records),
            files_touched=list(files_touched),
            token_usage=dict(token_usage or {}),
            output_text=(output_text or "")[:OUTPUT_TEXT_MAX],
            error_message=error_message,
            worktree_fallback=worktree_fallback,
            concurrency_group=concurrency_group,
        )
        artifact.risk_score = compute_risk_score(
            artifact, specs_by_name=specs_by_name or None
        )

        try:
            path = self._artifact_store.write(artifact)
        except Exception as exc:
            # Disk full, permissions, flaky NFS — none of these
            # should break the dispatch's LLM-visible return
            # payload. Log once and return a null ``artifact_path``.
            logger.warning(
                "artifact_store.write failed for %s: %s", args.role_name, exc
            )
            return {
                "artifact_id": artifact.artifact_id,
                "artifact_path": None,
            }

        # Fire the ``artifact.write`` event so replay / projections
        # see the side-effect even if someone later rotates the
        # artifact file away. The handle may be unwired in tests;
        # we tolerate that silently.
        if self._task_handle is not None:
            try:
                self._task_handle.append(
                    kind="artifact.write",
                    payload={
                        "artifact_id": artifact.artifact_id,
                        "role": artifact.role_name,
                        "success": artifact.success,
                        "stop_reason": artifact.stop_reason,
                        "risk_score": artifact.risk_score,
                        "duration_ms": artifact.duration_ms,
                        "artifact_path": str(path),
                    },
                    trace_id=artifact.parent_trace_id or None,
                )
            except Exception:
                logger.exception("artifact.write event emission failed")

        return {
            "artifact_id": artifact.artifact_id,
            "artifact_path": str(path),
        }

    async def _fire_sub_agent_end(
        self,
        child_trace_id: str,
        role_name: str,
        *,
        ok: bool,
        stop_reason: str | None,
    ) -> None:
        """Emit ``on_sub_agent_end`` exactly once per dispatch.

        Helper exists so every exit path in ``_dispatch_role_agent``
        (success / timeout / exception / failure) uniformly reports
        to lineage subscribers. Missing this fire causes a lineage
        tree to show "in flight" forever for a dispatch that already
        returned.
        """
        if self._hook_bus is None:
            return
        await self._hook_bus.afire(
            "on_sub_agent_end",
            {
                "parent_trace_id": self.trace_id,
                "child_trace_id": child_trace_id,
                "role": role_name,
                "ok": ok,
                "stop_reason": stop_reason,
            },
        )

    # ------------------------------------------------------------------
    # resume_last_dispatch
    # ------------------------------------------------------------------

    async def _resume_last_dispatch(
        self, args: ResumeLastDispatchArgs
    ) -> dict[str, Any]:
        """Re-dispatch the most recent sub-agent in THIS thread.

        Walks the per-thread event log (``task_handle.events()``)
        backwards to find the latest ``tool.call`` for
        ``dispatch_role_agent`` and its matching ``tool.result`` /
        ``tool.error``. Reconstructs the original dispatch args
        (role / task / acceptance / workflow_id), optionally lists
        any artifacts the previous sub-agent left on disk, and hands
        the new sub-agent a task description that asks it to READ
        those partials first rather than re-doing the work.

        Stays a pure wrapper: everything downstream — budget gate,
        thread updates, tool counts, observer — flows through the
        same ``_dispatch_role_agent`` path the LLM would call
        manually. Resume is just a bookkeeping shortcut.
        """
        if self._task_handle is None:
            return {
                "ok": False,
                "error": "NO_TASK_HANDLE",
                "message": (
                    "resume_last_dispatch requires per-thread task "
                    "logging, which is not wired in this session. "
                    "Re-issue the dispatch manually with "
                    "dispatch_role_agent."
                ),
            }

        try:
            events = self._task_handle.events()
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "resume_last_dispatch: failed to read events task_id=%s",
                self._task_handle.task_id,
                exc_info=True,
            )
            return {
                "ok": False,
                "error": "EVENT_LOG_UNREADABLE",
                "message": f"Failed to read thread event log: {exc}",
            }

        last_call_ev = None
        last_call_args: dict[str, Any] | None = None
        last_call_id: str | None = None
        for ev in reversed(events):
            if ev.kind != "tool.call":
                continue
            payload = ev.payload or {}
            if payload.get("tool_name") != "dispatch_role_agent":
                continue
            preview = payload.get("args_preview") or ""
            try:
                parsed_args = json.loads(preview) if preview else {}
            except json.JSONDecodeError:
                parsed_args = {}
            if not isinstance(parsed_args, dict):
                continue
            last_call_ev = ev
            last_call_args = parsed_args
            last_call_id = payload.get("call_id")
            break

        if last_call_ev is None or last_call_args is None:
            return {
                "ok": False,
                "error": "NO_PRIOR_DISPATCH",
                "message": (
                    "No prior dispatch_role_agent call was found in "
                    "this thread. Use dispatch_role_agent to make the "
                    "first dispatch."
                ),
            }

        last_result_kind: str | None = None
        last_result_preview: str = ""
        if last_call_id:
            for ev in events:
                if ev.seq <= last_call_ev.seq:
                    continue
                if ev.kind not in ("tool.result", "tool.error"):
                    continue
                payload = ev.payload or {}
                if payload.get("call_id") != last_call_id:
                    continue
                last_result_kind = ev.kind
                last_result_preview = str(payload.get("result_preview") or "")
                break

        last_run_was_ok = last_result_kind == "tool.result" and (
            '"success": true' in last_result_preview.lower()
            or ('"error"' not in last_result_preview and last_result_kind == "tool.result")
        )
        if last_run_was_ok and not args.force:
            return {
                "ok": False,
                "error": "LAST_DISPATCH_SUCCEEDED",
                "message": (
                    "The most recent dispatch_role_agent call reported "
                    "success; there is nothing to resume. If you want "
                    "another pass anyway, retry with force=true, or "
                    "issue a fresh dispatch_role_agent."
                ),
                "last_dispatch_args": last_call_args,
            }

        role_name = str(last_call_args.get("role_name") or "").strip()
        original_task = str(last_call_args.get("task") or "").strip()
        acceptance = str(last_call_args.get("acceptance_criteria") or "")
        workflow_id = str(last_call_args.get("workflow_id") or "")
        if not role_name or not original_task:
            return {
                "ok": False,
                "error": "PRIOR_DISPATCH_UNREADABLE",
                "message": (
                    "Found a prior dispatch_role_agent call but its "
                    "args were truncated beyond recovery. Re-issue "
                    "dispatch_role_agent manually."
                ),
            }

        artifact_lines: list[str] = []
        if (
            workflow_id
            and self._workflow is not None
            and self.project_id
        ):
            try:
                listing = self._workflow.list_artifacts(
                    workflow_id=workflow_id,
                    agent_name=self._workflow_agent_name,
                    project_id=self.project_id,
                    enforce_agent=False,
                )
            except Exception:  # noqa: BLE001 — best-effort hint
                listing = None
            if isinstance(listing, dict) and listing.get("exists"):
                for entry in listing.get("entries") or []:
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("type") != "file":
                        continue
                    rel = entry.get("rel_path") or entry.get("name")
                    if rel:
                        artifact_lines.append(str(rel))

        resume_header = [
            "【续跑任务】上一次同一 thread 里派给你的活因超时或失败未完成。",
            f"原任务（{role_name}）如下：",
            original_task,
        ]
        if last_result_kind or last_result_preview:
            tail = last_result_preview[:400].strip()
            resume_header.append(
                f"上次结果：{last_result_kind or 'unknown'}"
                + (f" — {tail}" if tail else "")
            )
        if artifact_lines:
            top = artifact_lines[:10]
            resume_header.append(
                "磁盘上已有以下 partial artifact（来自上次产出），"
                "请用 read_workflow_artifact / read_repo_file 读回来后"
                "**在此基础上续写**，不要从零重做：\n- "
                + "\n- ".join(top)
            )
        else:
            resume_header.append(
                "磁盘上未发现既有 artifact；若此前已落盘但未列出，"
                "请用 list_workflow_artifacts 主动检查一次再决定从哪里续。"
            )
        if args.extra_context.strip():
            resume_header.append(f"追加说明：{args.extra_context.strip()}")

        augmented_task = "\n\n".join(resume_header)
        dispatch_args = DispatchRoleAgentArgs(
            role_name=role_name,
            task=augmented_task,
            acceptance_criteria=acceptance,
            workflow_id=workflow_id,
        )

        self._fire_thread_update(
            f"🔁 resume_last_dispatch → {role_name}"
            + (f"（workflow={workflow_id}）" if workflow_id else "")
            + (
                f"；识别到 {len(artifact_lines)} 份 partial artifact"
                if artifact_lines
                else "；未检出 partial"
            )
        )

        dispatch_result = await self._dispatch_role_agent(dispatch_args)
        return {
            "ok": True,
            "resumed_from": {
                "seq": last_call_ev.seq,
                "ts": last_call_ev.ts,
                "role_name": role_name,
                "workflow_id": workflow_id,
                "last_result_kind": last_result_kind,
            },
            "artifact_hints": artifact_lines,
            "dispatch_result": dispatch_result,
        }

    # ------------------------------------------------------------------
    # delegate_to_application_agent
    # ------------------------------------------------------------------

    def _tech_lead_feishu_at_markup(self) -> str:
        """Feishu rich-text snippet for @技术组长 when open_id is configured."""
        if not self._tech_lead_bot_open_id:
            return ""
        return f'<at user_id="{self._tech_lead_bot_open_id}">技术组长</at>'

    async def _delegate_to_application_agent(self, args: DelegateToApplicationAgentArgs) -> dict[str, Any]:
        if self._app_delegate_url:
            body = {
                "source": "tech_lead",
                "source_label": "技术组长",
                "role_name": self.role_name,
                "trace_id": self.trace_id or "",
                "project_id": self.project_id or "",
                "tech_lead_chat_id": self.chat_id or "",
                "tech_lead_bot_open_id": self._tech_lead_bot_open_id,
                "tech_lead_mention_display_name": "技术组长",
                "tech_lead_at_text": self._tech_lead_feishu_at_markup(),
                "target_agent_label": self._app_agent_label,
                "message": args.message,
            }
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(self._app_delegate_url, json=body)
                    resp.raise_for_status()
                return {
                    "sent": True,
                    "channel": "delegate_webhook",
                    "status_code": resp.status_code,
                    "chat_id": None,
                    "message_id": None,
                    "error": None,
                    "note": (
                        f"已投递至 {self._app_agent_label} 的接入通道；对方应在飞书内自行回复已收到技术组长委派并执行任务。"
                    ),
                }
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code if exc.response is not None else None
                return {
                    "sent": False,
                    "channel": "delegate_webhook",
                    "status_code": code,
                    "chat_id": None,
                    "message_id": None,
                    "error": str(exc),
                }
            except Exception as exc:
                return {
                    "sent": False,
                    "channel": "delegate_webhook",
                    "chat_id": None,
                    "message_id": None,
                    "error": str(exc),
                }

        if not self._app_agent_group_chat_id:
            return {
                "sent": False,
                "error": (
                    "Neither application_agent_delegate_url nor application_agent_group_chat_id is configured."
                ),
            }

        if self._app_agent_open_id:
            text = f'<at user_id="{self._app_agent_open_id}">{self._app_agent_label}</at> {args.message}'
        else:
            text = args.message

        # Preferred path: "send as user" via impersonation. Feishu does
        # not deliver bot-to-bot @ events, so a tech-lead bot IM cannot
        # wake the delegate bot. A user_access_token makes the message
        # look like it came from the authorizing human, which those bots
        # typically respond to.
        user_token: str | None = None
        impersonation_error: str | None = None
        if self._impersonation_token_service is not None:
            try:
                user_token = await self._impersonation_token_service.get_access_token()
            except Exception as exc:
                impersonation_error = f"impersonation token call failed: {exc}"
                logger.exception("impersonation: unexpected failure")
            if not user_token:
                impersonation_error = (
                    self._impersonation_token_service.last_error
                    or impersonation_error
                    or "impersonation token unavailable"
                )

        if user_token and self._app_agent_open_id:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(
                        "https://open.feishu.cn/open-apis/im/v1/messages",
                        params={"receive_id_type": "chat_id"},
                        headers={
                            "Authorization": f"Bearer {user_token}",
                            "Content-Type": "application/json; charset=utf-8",
                        },
                        json={
                            "receive_id": self._app_agent_group_chat_id,
                            "msg_type": "text",
                            "content": json.dumps({"text": text}, ensure_ascii=False),
                        },
                    )
                payload = resp.json() if resp.content else {}
                data = payload.get("data") or {}
                code = payload.get("code")
                if code not in (0, None):
                    return {
                        "sent": False,
                        "channel": "feishu_im_as_user",
                        "chat_id": self._app_agent_group_chat_id,
                        "message_id": None,
                        "error": f"feishu code={code} msg={payload.get('msg')}",
                    }
                return {
                    "sent": True,
                    "channel": "feishu_im_as_user",
                    "chat_id": self._app_agent_group_chat_id,
                    "message_id": data.get("message_id"),
                    "error": None,
                }
            except Exception as exc:
                return {
                    "sent": False,
                    "channel": "feishu_im_as_user",
                    "chat_id": self._app_agent_group_chat_id,
                    "message_id": None,
                    "error": str(exc),
                }

        # Fallback: bot-IM (kept so the tool degrades gracefully when
        # no impersonation token is configured). Note this path will
        # NOT trigger OpenClaw for the target bot — it is mostly useful
        # for local dev / tests / human-owned targets.
        if not self._feishu_client:
            return {"sent": False, "error": "Feishu client not available."}

        try:
            payload = await self._feishu_client.request(
                "POST",
                "/open-apis/im/v1/messages?receive_id_type=chat_id",
                json_body={
                    "receive_id": self._app_agent_group_chat_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
            )
            message_id = payload.get("message_id") or payload.get("data", {}).get("message_id")
            result: dict[str, Any] = {
                "sent": True,
                "channel": "feishu_im",
                "chat_id": self._app_agent_group_chat_id,
                "message_id": message_id,
                "error": None,
            }
            if impersonation_error:
                result["impersonation_warning"] = impersonation_error
            return result
        except Exception as exc:
            return {
                "sent": False,
                "channel": "feishu_im",
                "chat_id": self._app_agent_group_chat_id,
                "message_id": None,
                "error": str(exc),
                "impersonation_warning": impersonation_error,
            }

    # ------------------------------------------------------------------
    # Retained tool handlers
    # ------------------------------------------------------------------

    def _contextualized_command_text(self) -> str:
        if not self.recent_conversation:
            return self.command_text

        lines = ["最近对话上下文："]
        for item in self.recent_conversation[-4:]:
            user_text = str(item.get("user_text") or "").strip()
            reply_text = str(item.get("reply_text") or "").strip()
            if user_text:
                lines.append(f"用户：{user_text}")
            if reply_text:
                lines.append(f"角色：{reply_text}")
        lines.append(f"当前用户消息：{self.command_text}")
        return "\n".join(lines)

    def _advance_sprint_state(self, args: AdvanceSprintStateArgs) -> dict[str, Any]:
        # ``ProgressSyncService`` is rooted at the agent repo; it can't
        # resolve ``sprint-status.yaml`` for projects whose source lives
        # in a separate ``project_repo_root``. Hand ``SprintStateService``
        # an empty records list — it will fall back to picking the next
        # story from its own correctly-rooted status data.
        changes = self._sprint_state.advance(
            [],
            story_key=args.story_key,
            to_status=args.to_status,
            reason=f"Feishu tool call: {self._contextualized_command_text()}",
            dry_run=args.dry_run,
        )
        story_key = changes[0].story_key if changes else (args.story_key or "")
        from_status = changes[0].from_status if changes else ""
        to_status = changes[0].to_status if changes else (args.to_status or "")
        return {
            "story_key": story_key,
            "from_status": from_status,
            "to_status": to_status,
            "dry_run": args.dry_run,
            "changes": [change.model_dump() for change in changes],
        }
