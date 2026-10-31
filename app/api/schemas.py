import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

WorkspaceRole = Literal["owner", "editor", "viewer"]


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 token type, not a credential
    expires_in: int
    refresh_token: str
    refresh_expires_in: int


class RefreshRequest(BaseModel):
    # Deliberately loose: a JWT is longer than a refresh token, and a wrong
    # credential must be refused by the auth check (401), not by the schema.
    refresh_token: str = Field(min_length=1, max_length=4096)


class RefreshTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 token type, not a credential
    expires_in: int
    refresh_token: str
    refresh_expires_in: int


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
    can_delete: bool = False
    member_count: int = 0


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


class AdminWorkspaceResponse(BaseModel):
    id: UUID
    slug: str
    name: str
    description: str | None
    created_at: datetime
    member_count: int
    members: list[WorkspaceMemberResponse] = []


class AdminWorkspaceListResponse(BaseModel):
    items: list[AdminWorkspaceResponse]
    total: int
    offset: int
    limit: int


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
    created_by_user_id: UUID | None
    created_at: datetime
    duplicate: bool = False
    row_count: int = 0


class DocumentListResponse(BaseModel):
    items: list[DocumentResponse]
    total: int
    offset: int
    limit: int


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
    row_count: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class ProcessingActivityResponse(BaseModel):
    items: list[ProcessingRunSummary]
    total: int


class DocumentSearchHit(BaseModel):
    document_id: UUID
    workspace_id: UUID
    workspace_name: str
    filename: str
    media_type: str
    size_bytes: int
    status: str
    row_count: int
    created_at: datetime


class DocumentSearchResponse(BaseModel):
    items: list[DocumentSearchHit]
    total: int


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("API key name cannot be blank")
        return normalized


class ApiKeySummary(BaseModel):
    id: UUID
    name: str
    key_prefix: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiKeyListResponse(BaseModel):
    items: list[ApiKeySummary]


class ApiKeyCreatedResponse(ApiKeySummary):
    key: str
    warning: str
