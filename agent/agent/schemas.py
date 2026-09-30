from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class AgentHistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=24_000)
    source_workspace_ids: list[UUID] = Field(default_factory=list, max_length=1000)


class AnswerRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    scope_token: str = Field(min_length=20, max_length=65_536)
    history: list[AgentHistoryMessage] = Field(default_factory=list, max_length=24)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Question cannot be blank")
        return normalized


class ContextSource(BaseModel):
    workspace_id: UUID
    document_id: UUID
    processing_run_id: UUID
    chunk_id: UUID
    ordinal: int
    score: float
    content_hash: str
    filename: str | None
    section_path: list[str]
    page_numbers: list[int]
    text: str


class AnswerResponse(BaseModel):
    answer: str = Field(min_length=1, max_length=24_000)
    sources: list[ContextSource] = Field(max_length=32)
