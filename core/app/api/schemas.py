from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class EventAcceptedResponse(BaseModel):
    accepted: bool = True
    duplicate: bool
    event_id: UUID


class ProcessingRunResponse(BaseModel):
    id: UUID
    job_kind: str
    pipeline_version: str
    status: str
    attempts: int
    chunk_count: int
    embedded_chunk_count: int
    embedding_profile: str | None
    embedding_model: str | None
    embedding_dimensions: int | None
    last_error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class ProcessingRunSummary(BaseModel):
    id: UUID
    workspace_id: UUID
    document_id: UUID
    filename: str | None
    job_kind: str
    status: str
    attempts: int
    chunk_count: int
    embedded_chunk_count: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class ActiveProcessingRunResponse(BaseModel):
    item: ProcessingRunSummary | None


class ProcessingRunScopeRequest(BaseModel):
    workspace_ids: list[UUID] = Field(min_length=1, max_length=1000)

    @field_validator("workspace_ids")
    @classmethod
    def unique_workspace_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("Workspace IDs must be unique")
        return value


class ProcessingRunListResponse(BaseModel):
    items: list[ProcessingRunSummary]
    total: int
    since: datetime


class DocumentStatusResponse(BaseModel):
    workspace_id: UUID
    document_id: UUID
    original_filename: str | None
    media_type: str | None
    sha256: str
    active_processing_run_id: UUID | None
    deleted_at: datetime | None
    runs: list[ProcessingRunResponse]


class ChunkResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    document_id: UUID
    processing_run_id: UUID
    ordinal: int
    text: str
    token_count: int
    section_path: list[str]
    page_numbers: list[int]
    provenance: dict[str, Any]
    content_hash: str


class ChunkListResponse(BaseModel):
    items: list[ChunkResponse]
    total: int
    offset: int
    limit: int


class SemanticSearchRequest(BaseModel):
    workspace_ids: list[UUID] = Field(min_length=1, max_length=1000)
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=8, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def non_whitespace_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Search query cannot be blank")
        return normalized

    @field_validator("workspace_ids")
    @classmethod
    def unique_workspace_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("Workspace IDs must be unique")
        return value


class SemanticSearchResult(BaseModel):
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


class SemanticSearchResponse(BaseModel):
    items: list[SemanticSearchResult]
