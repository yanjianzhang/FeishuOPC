from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from feishu_agent.core.agent_types import AgentToolSpec


class ResolveBitableTargetArgs(BaseModel):
    table_name: str | None = Field(default=None, max_length=255)
    require_write: bool = False


class SyncProgressArgs(BaseModel):
    module: str | None = Field(default=None, max_length=255)
    table_name: str | None = Field(default=None, max_length=255)


class ReadBitableSchemaArgs(BaseModel):
    table_name: str | None = Field(default=None, max_length=255)


class ReadBitableRowsArgs(BaseModel):
    table_name: str | None = Field(default=None, max_length=255)
    search_text: str | None = Field(default=None, max_length=255)
    field_names: list[str] | None = Field(default=None, max_length=20)
    page_token: str | None = Field(default=None, max_length=255)
    limit: int = Field(default=20, ge=1, le=100)


class SprintArgs(BaseModel):
    sprint: str | None = Field(default="current", max_length=255)


class AdvanceSprintStateArgs(BaseModel):
    story_key: str | None = Field(default=None, max_length=255)
    to_status: Literal["planned", "in-progress", "review", "done", "blocked"] | None = None
    dry_run: bool = False


class WriteFileArgs(BaseModel):
    path: str = Field(description="Relative file path within the allowed write root (e.g. 'my-feature/prd.md')", min_length=1, max_length=512)
    content: str = Field(description="UTF-8 text content to write to the file")


class RequestConfirmationArgs(BaseModel):
    # ``advance_sprint_state`` was intentionally removed from this
    # Literal. Moving a story between planned / in-progress / review /
    # done is a single YAML field flip: fully reversible, audit-logged
    # in ``techbot-runs`` and traceable via git. Gating it behind a
    # confirm round-trip produced a worse UX than letting TL run it
    # directly (users had to say "推进下一个 sprint" then "确认" again,
    # and stale pending files occasionally hijacked fresh intents).
    # TL is instructed to call ``advance_sprint_state`` straight away;
    # only genuinely hard-to-undo Bitable writes go through here.
    action_type: Literal["write_progress_sync"] = Field(
        description="The destructive action that needs confirmation before execution"
    )
    action_args: dict[str, object] = Field(
        default_factory=dict,
        description="Arguments that will be passed to the action when confirmed (e.g. module)",
    )
    summary: str = Field(
        description="Human-readable Chinese summary of what this action will do, shown to the user for confirmation",
    )


def _tool_spec(name: str, description: str, args_model: type[BaseModel]) -> AgentToolSpec:
    schema = args_model.model_json_schema()
    schema["additionalProperties"] = False
    return AgentToolSpec(name=name, description=description, input_schema=schema)


TECH_LEAD_TOOL_SPECS = [
    _tool_spec(
        "read_sprint_status",
        "Read the current sprint status file and return the goal plus current sprint lists.",
        SprintArgs,
    ),
    _tool_spec(
        "advance_sprint_state",
        "Advance one sprint story to the next status or to an explicitly requested status.",
        AdvanceSprintStateArgs,
    ),
]
