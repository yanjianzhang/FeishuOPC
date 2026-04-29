from typing import Literal

from pydantic import BaseModel, Field


class FeishuTextContent(BaseModel):
    text: str


class FeishuSendMessageRequest(BaseModel):
    receive_id: str = Field(..., min_length=1, max_length=255)
    msg_type: Literal["text"] = "text"
    content: FeishuTextContent
