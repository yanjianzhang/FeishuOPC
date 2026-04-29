"""Tool bundles for the A-2 GenericRoleExecutor surface.

Each submodule exports a ``build_<name>_bundle(ctx: BundleContext)``
factory that returns a list of ``(AgentToolSpec, Handler)`` pairs.
The factories are pure functions of their ``BundleContext`` — they
never mutate the registry or hold process-global state.

The canonical source of a tool's ``name`` / ``description`` /
``input_schema`` is the existing mixin / executor module it was
extracted from; bundles import that spec and ``dataclasses.replace``
it to attach the correct ``effect`` / ``target`` metadata that
M3 role filtering relies on. This preserves exactly one copy of
the LLM-visible contract per tool.

Registering all bundles at once
-------------------------------
At process start, call :func:`register_builtin_bundles` to populate
a :class:`BundleRegistry` with every shipped bundle. Individual
callers can still register extras (e.g. an experimental bundle) after.
"""

from __future__ import annotations

from feishu_agent.tools.bundle_registry import BundleRegistry
from feishu_agent.tools.bundles.bitable_read import build_bitable_read_bundle
from feishu_agent.tools.bundles.bitable_write import build_bitable_write_bundle
from feishu_agent.tools.bundles.feishu_chat import build_feishu_chat_bundle
from feishu_agent.tools.bundles.fs_read import build_fs_read_bundle
from feishu_agent.tools.bundles.fs_write import build_fs_write_bundle
from feishu_agent.tools.bundles.git_local import build_git_local_bundle
from feishu_agent.tools.bundles.git_remote import build_git_remote_bundle
from feishu_agent.tools.bundles.search import build_search_bundle
from feishu_agent.tools.bundles.sprint import build_sprint_bundle


def register_builtin_bundles(registry: BundleRegistry) -> None:
    """Register every shipped bundle factory on ``registry``.

    Call this exactly once per process. Raises
    :class:`feishu_agent.tools.bundle_registry.BundleNotFoundError`
    or ``ValueError`` (duplicate) if invoked twice on the same registry.
    """
    registry.register("sprint", build_sprint_bundle)
    registry.register("bitable_read", build_bitable_read_bundle)
    registry.register("bitable_write", build_bitable_write_bundle)
    registry.register("fs_read", build_fs_read_bundle)
    registry.register("fs_write", build_fs_write_bundle)
    registry.register("git_local", build_git_local_bundle)
    registry.register("git_remote", build_git_remote_bundle)
    registry.register("search", build_search_bundle)
    registry.register("feishu_chat", build_feishu_chat_bundle)


__all__ = [
    "build_bitable_read_bundle",
    "build_bitable_write_bundle",
    "build_feishu_chat_bundle",
    "build_fs_read_bundle",
    "build_fs_write_bundle",
    "build_git_local_bundle",
    "build_git_remote_bundle",
    "build_search_bundle",
    "build_sprint_bundle",
    "register_builtin_bundles",
]
