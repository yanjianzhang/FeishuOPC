"""Bundle: ``feishu_chat`` (placeholder)

Tool surface: *currently empty*.

Why empty
---------
The A-2 spec lists ``request_confirmation`` and ``notify_tech_lead``
here. Neither is MVP-ready for the GenericRoleExecutor path:

- ``request_confirmation`` exists only on ``TechLeadToolExecutor`` and
  relies on a :class:`PendingActionService` that is not yet exposed
  through :class:`BundleContext`. Shipping the tool without the
  dependent service would let role frontmatter declare a tool whose
  handler returns an error on every call — worse UX than omitting
  the tool. TL stays a custom executor in A-2, so TL itself keeps
  full access to the tool via its existing wiring; only bundle-
  consumers would observe the gap.
- ``notify_tech_lead`` has no backing service yet.

Reserving the bundle name keeps role frontmatter stable once the
backing pieces land (tracked in A-3 / post-A-2 follow-up):
role authors can reference ``feishu_chat`` today without an error
because the bundle factory is registered and simply returns ``[]``
until the service pointers arrive.
"""

from __future__ import annotations

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.tools.bundle_context import BundleContext
from feishu_agent.tools.bundle_registry import Handler


def build_feishu_chat_bundle(
    _ctx: BundleContext,
) -> list[tuple[AgentToolSpec, Handler]]:
    """Return an empty list until ``request_confirmation`` /
    ``notify_tech_lead`` are lifted into the bundle layer.

    Declared as a registered factory so role frontmatter using
    ``tool_bundles: [feishu_chat]`` passes bundle resolution. A role
    that relies on this bundle for critical surface will see an
    empty ``tool_specs()`` and fail at LLM level with "no tool
    available" — the same symptom as declaring an unimplemented
    tool, but without a dispatch-time crash.
    """
    return []
