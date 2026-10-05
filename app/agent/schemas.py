from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class AgentHistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=24_000)
    source_workspace_ids: list[UUID] = Field(default_factory=list, max_length=1000)


class AnswerRequest(BaseModel):
    """Internal shape for a direct harness call.

    The scope token is gone: the caller derives ``workspace_ids`` from
    the caller's own memberships and passes them in, so there is no
    signature to verify and nothing to forge. The retriever still refuses
    an empty or duplicated scope.
    """

    question: str = Field(min_length=1, max_length=2000)
    workspace_ids: list[UUID] = Field(min_length=1, max_length=1000)
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
    token_count: int = 0


class AnswerResponse(BaseModel):
    answer: str = Field(min_length=1, max_length=24_000)
    sources: list[ContextSource] = Field(max_length=32)
