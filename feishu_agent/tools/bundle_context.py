"""Runtime context handed to every tool-bundle factory.

A bundle factory receives exactly one object: :class:`BundleContext`.
It reads whichever fields it needs (``working_dir``, ``sprint_service``,
``progress_sync_service`` …) and returns a list of
``(AgentToolSpec, handler)`` pairs.

The context is a frozen dataclass so a bundle can't mutate it; the
*services* it points to can still be invoked (they hold their own
internal state). Optional fields default to ``None`` so unit tests can
build a BundleContext without wiring a real :class:`ManagedFeishuClient`
or :class:`SprintStateService`.

Worktree awareness
------------------
``working_dir`` is NOT always ``repo_root``. For roles with
``needs_worktree=True`` (developer / bug_fixer, B-3), ``working_dir``
points at ``.worktrees/{trace}/``. Tools that read/write project code
MUST use ``working_dir`` so parallel code-writing agents stay isolated.
Tools that push to remote (git_push, create_pull_request …) MUST use
``repo_root`` and serialize via ``repo_filelock`` because there is only
one remote-push-safe working tree per repo.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from feishu_agent.runtime.managed_feishu_client import ManagedFeishuClient
    from feishu_agent.team.audit_service import AuditService
    from feishu_agent.team.role_artifact_writer import RoleArtifactWriter
    from feishu_agent.team.sprint_state_service import SprintStateService
    from feishu_agent.team.task_service import TaskHandle
    from feishu_agent.tools.ci_watch_service import CIWatchService
    from feishu_agent.tools.code_write_service import CodeWriteService
    from feishu_agent.tools.git_ops_service import GitOpsService
    from feishu_agent.tools.pre_push_inspector import PrePushInspector
    from feishu_agent.tools.progress_sync_service import ProgressSyncService
    from feishu_agent.tools.pull_request_service import PullRequestService
    from feishu_agent.tools.workflow_service import WorkflowService


@dataclass(frozen=True, eq=False)
class BundleContext:
    """Per-dispatch context consumed by every :class:`BundleFactory`.

    Two filesystem paths are tracked separately because B-3 worktree
    isolation splits the scope of "code write" (worktree-local) from
    "push remote" (repo-root-only). Bundle factories should pick the
    right one: ``working_dir`` for local operations, ``repo_root`` for
    anything that must serialize against the single remote.

    ``eq=False`` is deliberate: two semantically-equivalent contexts
    constructed from different callable defaults would otherwise
    compare unequal (lambdas hash by identity), and nothing in the
    dispatch path benefits from structural equality / hashing. Users
    who need identity comparison can still use ``ctx is other``.
    """

    working_dir: Path
    repo_root: Path

    chat_id: str
    trace_id: str
    role_name: str

    # Wiring passthroughs consumed by existing services (audit, write
    # authorization, etc). Empty strings preserve today's "no wiring"
    # default so pure unit tests need not stub a full runtime.
    project_id: str = ""
    command_text: str = ""

    # Wave 1 services.
    sprint_service: "SprintStateService | None" = None
    progress_sync_service: "ProgressSyncService | None" = None
    feishu_client: "ManagedFeishuClient | None" = None
    audit_service: "AuditService | None" = None
    task_handle: "TaskHandle | None" = None

    # Wave 2 services. These back the bundles migrated out of the
    # per-role executor classes. All are optional so bundle factories
    # can degrade gracefully (a bundle whose backing service is None
    # simply returns fewer specs, matching today's CodeWriteToolsMixin
    # "advertise only what you wired" semantics).
    code_write_service: "CodeWriteService | None" = None
    git_ops_service: "GitOpsService | None" = None
    pull_request_service: "PullRequestService | None" = None
    pre_push_inspector: "PrePushInspector | None" = None
    ci_watch_service: "CIWatchService | None" = None
    workflow_service: "WorkflowService | None" = None
    role_artifact_writer: "RoleArtifactWriter | None" = None

    # Optional UX streaming callback. Current mixins emit "📝 git
    # commit branch@sha …" lines via a host-provided callback; bundles
    # keep that UX by calling ctx.thread_update_fn if set.
    thread_update_fn: Callable[[str], Any] | None = None

    # Role-aware Bitable routing. The current handlers resolve a
    # Bitable target by consulting role permissions; the callbacks
    # below let bundles keep that behavior without pulling config
    # loading into every bundle file. Defaults return empty mappings
    # so test code can build a context with zero wiring.
    load_bitable_tables: Callable[[], dict[str, Any]] = lambda: {}
    load_role_permissions: Callable[[str], list[Any]] = lambda _role: []
    build_progress_sync_service_for_target: (
        Callable[[str | None, Any | None], "ProgressSyncService"] | None
    ) = None
