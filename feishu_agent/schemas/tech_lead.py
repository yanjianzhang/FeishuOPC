from pydantic import BaseModel, Field


class TechLeadStateChange(BaseModel):
    story_key: str
    from_status: str
    to_status: str
    reason: str
    locations: list[str] = Field(default_factory=list)
