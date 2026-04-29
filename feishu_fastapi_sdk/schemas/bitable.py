from typing import Any

from pydantic import BaseModel, Field


class BitableWriteFailure(BaseModel):
    code: str
    message: str
    external_key: str | None = None
    row: dict[str, Any] | None = None


class BitableWriteResult(BaseModel):
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list[BitableWriteFailure] = Field(default_factory=list)
