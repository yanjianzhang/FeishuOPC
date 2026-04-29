"""Role-executor registration.

Two registration paths coexist after A-2 Wave 3:

1. **Legacy factory table** — the remaining six class-based role
   executors (``developer`` / ``bug_fixer`` / ``deploy_engineer`` /
   ``progress_sync`` / ``reviewer`` / ``prd_writer``) are registered
   by name via :meth:`RoleRegistryService.register_executor_factory`
   and instantiated by the runtime's
   :func:`_build_role_executor_provider`. These roles either keep
   custom per-role logic (developer / bug_fixer / deploy_engineer's
   code-write / git / deploy pipelines) or write outside the
   ``role_artifact_writer`` tree (``prd_writer`` → ``specs/``), so
   they are NOT migrated to the bundle-driven path.

2. **Bundle composition** — six roles (``repo_inspector`` /
   ``researcher`` / ``spec_linker`` / ``ux_designer`` / ``qa_tester``
   / ``sprint_planner``) declare ``tool_bundles: [...]`` in their
   frontmatter and are served by
   :class:`feishu_agent.roles.generic_role_executor.GenericRoleExecutor`.
   Their Python classes were deleted in Wave 3; the runtime's
   provider builds a ``BundleContext`` + ``GenericRoleExecutor``
   whenever the resolved :class:`RoleDefinition` has a non-empty
   ``tool_bundles`` list. The provider falls through to the legacy
   factory table below only when that list is empty.

3. **Decorator autodiscover** — new tools declared with
   :func:`feishu_agent.tools.tool_registry.tool` are picked up by
   scanning ``feishu_agent.tools.legacy_tools``. Autodiscovery is
   invoked from :func:`register_role_executors` so operators only
   have one entry point to remember.

Dropping a new file under ``feishu_agent/tools/legacy_tools/<topic>.py``
is enough to expose new tools; no edit here is needed.
"""

from __future__ import annotations

from feishu_agent.roles.role_registry_service import RoleRegistryService
from feishu_agent.tools.tool_registry import autodiscover

from .bug_fixer import BugFixerExecutor
from .deploy_engineer import DeployEngineerExecutor
from .developer import DeveloperExecutor
from .prd_writer import PrdWriterExecutor
from .progress_sync import ProgressSyncExecutor
from .reviewer import ReviewerExecutor

# Table of legacy role factories. Six roles were migrated to the
# BundleRegistry path in A-2 Wave 3 and are NOT present here —
# ``_build_role_executor_provider`` routes them to
# :class:`GenericRoleExecutor` based on the role's ``tool_bundles``
# frontmatter. The only entries that remain are roles whose tool
# surface still requires hand-written composition (code-write, git,
# deploy) or that write to paths outside ``role_artifact_writer``'s
# ``docs/`` convention (``prd_writer`` → ``specs/``).
LEGACY_ROLE_FACTORIES: dict[str, type] = {
    "progress_sync": ProgressSyncExecutor,
    "reviewer": ReviewerExecutor,
    "prd_writer": PrdWriterExecutor,
    "developer": DeveloperExecutor,
    "bug_fixer": BugFixerExecutor,
    "deploy_engineer": DeployEngineerExecutor,
}


def register_role_executors(registry: RoleRegistryService) -> None:
    """Register the shipped legacy role executors + trigger tool
    autodiscovery.

    Autodiscovery fires once at startup so the
    :data:`feishu_agent.tools.tool_registry.GLOBAL_TOOL_REGISTRY`
    is populated with every ``@tool`` decorated function before any
    LLM session starts. This is safe to call repeatedly — the
    registry deduplicates by tool name.

    Bundle-driven roles are discovered separately from their role
    markdown (via the runtime's BundleRegistry) — they do not appear
    in ``LEGACY_ROLE_FACTORIES`` and therefore need no registration
    step here.
    """
    for name, factory in LEGACY_ROLE_FACTORIES.items():
        registry.register_executor_factory(name, factory)

    autodiscover(["feishu_agent.tools.legacy_tools"])


__all__ = [
    "register_role_executors",
    "BugFixerExecutor",
    "DeployEngineerExecutor",
    "DeveloperExecutor",
    "ProgressSyncExecutor",
    "ReviewerExecutor",
    "PrdWriterExecutor",
]
