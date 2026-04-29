"""Bundle: ``sprint``

Tool surface:
- ``read_sprint_status`` (effect=read, target=read.sprint)
- ``advance_sprint_state`` (effect=self, target=self.sprint)

Backing service: :class:`feishu_agent.team.sprint_state_service.SprintStateService`.

Rationale for effects: reading sprint YAML is pure read; advancing
story state flips a single field in ``sprint-status.yaml`` and is
fully reversible / audit-logged, so it counts as ``self`` (affects
only the team's own state, not any user-visible world). This mirrors
the decision recorded in ``RequestConfirmationArgs`` docstring to
NOT gate ``advance_sprint_state`` behind a human-confirm step.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from feishu_agent.core.agent_types import AgentToolSpec
from feishu_agent.tools.bundle_context import BundleContext
from feishu_agent.tools.bundle_registry import Handler
from feishu_agent.tools.feishu_agent_tools import (
    AdvanceSprintStateArgs,
    SprintArgs,
    _tool_spec,
)

# Base specs (no M3 metadata). Reused across tech_lead and sub-agents,
# so we keep the canonical definition here rather than rebuilding it
# per role. ``dataclasses.replace`` below attaches bundle-level
# effect/target metadata.
_READ_SPRINT_STATUS_BASE = _tool_spec(
    "read_sprint_status",
    "Read the current sprint status file and return the goal plus current sprint lists.",
    SprintArgs,
)
_ADVANCE_SPRINT_STATE_BASE = _tool_spec(
    "advance_sprint_state",
    "Advance one sprint story to the next status or to an explicitly requested status.",
    AdvanceSprintStateArgs,
)


def build_sprint_bundle(ctx: BundleContext) -> list[tuple[AgentToolSpec, Handler]]:
    """Return ``[(spec, handler)]`` pairs for the sprint bundle.

    If :attr:`BundleContext.sprint_service` is ``None`` returns an
    empty list — the role definition should not have declared the
    bundle if no sprint service is wired, but we degrade gracefully
    rather than crashing at role init.
    """
    sprint_service = ctx.sprint_service
    if sprint_service is None:
        return []

    read_spec = replace(
        _READ_SPRINT_STATUS_BASE, effect="read", target="read.sprint"
    )
    advance_spec = replace(
        _ADVANCE_SPRINT_STATE_BASE, effect="self", target="self.sprint"
    )

    def _handle_read(arguments: dict[str, Any]) -> dict[str, Any]:
        parsed = SprintArgs.model_validate(arguments)
        _ = parsed.sprint  # reserved for future multi-sprint support
        data = sprint_service.load_status_data()
        current_sprint = data.get("current_sprint") or {}
        return {
            "sprint_name": data.get("sprint_name"),
            "goal": current_sprint.get("goal"),
            "current_sprint": current_sprint,
        }

    def _handle_advance(arguments: dict[str, Any]) -> dict[str, Any]:
        parsed = AdvanceSprintStateArgs.model_validate(arguments)
        # Skip the ProgressSyncService read_records path like the
        # existing TL / sprint_planner handlers: SprintStateService
        # picks the next story from its own correctly-rooted status
        # data when passed an empty records list.
        reason = (
            f"{ctx.role_name} tool call: {ctx.command_text}"
            if ctx.command_text
            else f"{ctx.role_name} tool call"
        )
        changes = sprint_service.advance(
            [],
            story_key=parsed.story_key,
            to_status=parsed.to_status,
            reason=reason,
            dry_run=parsed.dry_run,
        )
        story_key = changes[0].story_key if changes else (parsed.story_key or "")
        from_status = changes[0].from_status if changes else ""
        to_status = changes[0].to_status if changes else (parsed.to_status or "")
        return {
            "story_key": story_key,
            "from_status": from_status,
            "to_status": to_status,
            "dry_run": parsed.dry_run,
            "changes": [change.model_dump() for change in changes],
        }

    return [
        (read_spec, _handle_read),
        (advance_spec, _handle_advance),
    ]
