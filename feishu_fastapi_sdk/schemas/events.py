from typing import Any

from pydantic import BaseModel, Field


class FeishuEventHeader(BaseModel):
    event_id: str | None = None
    event_type: str | None = None


class FeishuMessage(BaseModel):
    chat_id: str | None = None
    content: Any = None


class FeishuEventPayload(BaseModel):
    message: FeishuMessage | None = None


class FeishuEventEnvelope(BaseModel):
    token: str | None = None
    type: str | None = None
    challenge: str | None = None
    header: FeishuEventHeader = Field(default_factory=FeishuEventHeader)
    event: FeishuEventPayload = Field(default_factory=FeishuEventPayload)


class FeishuUrlVerificationResponse(BaseModel):
    challenge: str
