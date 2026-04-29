from typing import Literal, Optional

from pydantic import BaseModel, Field


class FeishuCommandRoute(BaseModel):
    normalized_action: Literal[
        "sync_preview",
        "sync_write",
        "tech_analysis",
        "tech_execute",
        "tech_review",
        "tech_continue_sprint",
    ]
    manager_mode: Optional[Literal["analysis", "execute", "review"]] = None
    execution_mode: Literal["preview", "write", "manager"] = "preview"
    filters: dict[str, list[str] | None] = Field(
        default_factory=lambda: {"module": None, "status": None}
    )
    module: Optional[str] = None
    story_key: Optional[str] = None
    sprint: Optional[str] = None
    requires_manager: bool = False
    sync_after: bool = False
    mutate_state: bool = False
    dry_run: bool = True
    rationale: str = ""
