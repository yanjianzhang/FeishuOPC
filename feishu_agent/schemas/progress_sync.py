from typing import Any, Literal, Optional

from feishu_fastapi_sdk import BitableWriteResult
from pydantic import BaseModel, Field


class ProgressSyncRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)
    command_text: str = Field(..., min_length=1, max_length=2000)
    mode: Literal["preview", "write"] = "preview"
    auth_mode: Literal["auto", "tenant", "user"] = "auto"
    trace_id: Optional[str] = Field(default=None, max_length=255)
    chat_id: Optional[str] = Field(default=None, max_length=255)
    bitable_app_token: Optional[str] = Field(default=None, max_length=255)
    bitable_table_id: Optional[str] = Field(default=None, max_length=255)
    user_access_token: Optional[str] = Field(default=None, max_length=4096)


class ProgressRecord(BaseModel):
    external_key: str
    project_id: str
    record_type: str
    native_key: str
    status: str
    summary: str
    source_path: str
    project_type: Optional[str] = None
    story_key: Optional[str] = None
    module: Optional[str] = None
    title: Optional[str] = None
    owner: Optional[str] = None
    risk: Optional[str] = None
    spec_path: Optional[str] = None
    artifact_path: Optional[str] = None
    source_kind: Optional[str] = None
    updated_at: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    raw_status: Optional[str] = None


class ProgressSummary(BaseModel):
    total: int
    by_status: dict[str, int]


class ProgressSyncResponse(BaseModel):
    ok: bool
    mode: Literal["preview", "write"]
    project_id: str
    trace_id: str
    message: str
    summary: ProgressSummary
    records: list[ProgressRecord] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    write_result: Optional[BitableWriteResult] = None
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
