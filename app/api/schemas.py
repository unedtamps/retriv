import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent.schemas import ContextSource

WorkspaceRole = Literal["owner", "editor", "viewer"]


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 token type, not a credential
    expires_in: int


class CurrentUserResponse(BaseModel):
    id: UUID
    username: str
    role: str
    is_active: bool


class WorkspaceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    name: str
    description: str | None
    created_at: datetime
    membership_role: WorkspaceRole | None = None
    can_upload: bool = False
    can_download: bool = False
    can_search_context: bool = False
    can_manage_members: bool = False


class WorkspaceListResponse(BaseModel):
    items: list[WorkspaceResponse]
    total: int
    offset: int
    limit: int


class AdminUserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    role: Literal["admin", "user"] = "user"

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("Username cannot be blank")
        return normalized


class AdminUserResponse(BaseModel):
    id: UUID
    username: str
    role: str
    is_active: bool
    initial_password: str


class AdminUserListItemResponse(BaseModel):
    id: UUID
    username: str
    role: str
    is_active: bool
    created_at: datetime


class AdminUserListResponse(BaseModel):
    items: list[AdminUserListItemResponse]
    total: int
    offset: int
    limit: int


class AdminPasswordResponse(BaseModel):
    user_id: UUID
    initial_password: str


class WorkspaceCreateRequest(BaseModel):
    slug: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    owner_user_id: UUID

    @field_validator("slug")
    @classmethod
    def normalize_slug(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized):
            raise ValueError("Slug must contain lowercase words separated by hyphens")
        return normalized

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Workspace name cannot be blank")
        return normalized

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class AdminMemberRequest(BaseModel):
    role: WorkspaceRole


class OwnerMemberRequest(BaseModel):
    role: Literal["editor", "viewer"]


class WorkspaceMemberResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    username: str
    role: WorkspaceRole
    granted_by_user_id: UUID
    created_at: datetime
    updated_at: datetime


class WorkspaceMemberListResponse(BaseModel):
    items: list[WorkspaceMemberResponse]


class WorkspaceMemberCandidateResponse(BaseModel):
    id: UUID
    username: str


class WorkspaceMemberCandidateListResponse(BaseModel):
    items: list[WorkspaceMemberCandidateResponse]


class DocumentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    original_filename: str
    detected_media_type: str
    size_bytes: int
    sha256: str
    status: str
    # Nullable because a record that arrived from an external system has
    # no human uploader. The dashboard renders it as "system".
    created_by_user_id: UUID | None
    created_at: datetime
    duplicate: bool = False


class DocumentListResponse(BaseModel):
    items: list[DocumentResponse]
    total: int
    offset: int
    limit: int


class AskContextRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Question cannot be blank")
        return normalized


class AgentConversationCreateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        return normalized


class AgentConversationSummary(BaseModel):
    id: UUID
    title: str | None
    created_at: datetime
    updated_at: datetime


class AgentConversationListResponse(BaseModel):
    items: list[AgentConversationSummary]
    total: int
    offset: int
    limit: int


class AgentConversationTurnRequest(BaseModel):
    request_id: UUID
    question: str = Field(min_length=1, max_length=2000)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Question cannot be blank")
        return normalized


class ConversationSourceReference(BaseModel):
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


class AgentHistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=24_000)
    source_workspace_ids: list[UUID] = Field(default_factory=list, max_length=1000)


class AgentConversationTurnResponse(BaseModel):
    id: UUID
    request_id: UUID
    sequence: int
    question: str
    answer: str | None
    status: Literal["processing", "completed", "failed"]
    failure_code: str | None
    created_at: datetime
    completed_at: datetime | None
    context_available: bool = True
    sources: list[ConversationSourceReference] = Field(max_length=32)


class AgentConversationTurnListResponse(BaseModel):
    items: list[AgentConversationTurnResponse]
    total: int
    offset: int
    limit: int


class AgentAskResponse(BaseModel):
    """Public shape of an answer.

    ContextSource is the harness's own model, reused rather than
    redefined: the answer path no longer crosses an HTTP boundary, so
    there is no upstream response to re-validate into a parallel type.
    """

    answer: str = Field(min_length=1, max_length=24_000)
    sources: list[ContextSource] = Field(max_length=32)


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


class ProcessingRunListResponse(BaseModel):
    items: list[ProcessingRunSummary]
    total: int
    since: datetime


class ProblemDetail(BaseModel):
    type: str = "about:blank"
    title: str
    status: int
    detail: str
    code: str
    instance: str | None = None
    errors: list[dict[str, str]] = Field(default_factory=list)
