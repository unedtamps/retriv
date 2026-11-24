import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class SetupStatusResponse(BaseModel):
    needs_admin: bool
    setup_allowed: bool


class SetupAdminRequest(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=12, max_length=1024)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("Username cannot be blank")
        return normalized


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


VersionRelationKind = Literal["supersedes", "periodic_series"]


class DocumentEntityResponse(BaseModel):
    name: str
    type: str = "other"
    aliases: list[str] = Field(default_factory=list)


class DocumentProfileResponse(BaseModel):
    status: str
    title: str | None = None
    doc_category: str = "other"
    doc_subtype: str | None = None
    language: str | None = None
    primary_entities: list[DocumentEntityResponse] = Field(default_factory=list)
    counterparties: list[DocumentEntityResponse] = Field(default_factory=list)
    period_label: str | None = None
    effective_date: str | None = None
    document_number: str | None = None
    version_label: str | None = None
    summary: str | None = None
    topics: list[str] = Field(default_factory=list)
    model: str | None = None
    updated_at: datetime | None = None


class DocumentVersionSignalResponse(BaseModel):
    group_id: UUID
    group_name: str
    relation_kind: VersionRelationKind
    version_rank: int
    version_count: int
    is_latest: bool
    is_superseded: bool
    latest_document_id: UUID
    latest_filename: str
    latest_period_label: str | None = None
    version_note: str | None = None


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
    profile: DocumentProfileResponse | None = None
    version: DocumentVersionSignalResponse | None = None
    pending_version_suggestions: int = 0


class DocumentListResponse(BaseModel):
    items: list[DocumentResponse]
    total: int
    offset: int
    limit: int


class DocumentVersionMemberResponse(BaseModel):
    document_id: UUID
    filename: str
    title: str | None = None
    period_label: str | None = None
    effective_date: str | None = None
    version_label: str | None = None
    version_rank: int
    is_latest: bool
    version_note: str | None = None
    linked_at: datetime
    created_at: datetime


class DocumentVersionGroupResponse(BaseModel):
    group_id: UUID
    name: str
    relation_kind: VersionRelationKind
    members: list[DocumentVersionMemberResponse] = Field(default_factory=list)


class VersionSuggestionDocResponse(BaseModel):
    document_id: UUID
    filename: str
    title: str | None = None
    doc_category: str | None = None
    doc_subtype: str | None = None
    period_label: str | None = None
    effective_date: str | None = None
    version_label: str | None = None
    primary_entities: list[str] = Field(default_factory=list)
    created_at: datetime


class DocumentVersionSuggestionResponse(BaseModel):
    id: UUID
    workspace_id: UUID
    document_a: VersionSuggestionDocResponse
    document_b: VersionSuggestionDocResponse
    suggested_newer_document_id: UUID
    suggested_relation: VersionRelationKind
    confidence: float
    reasons: list[str] = Field(default_factory=list)
    status: str
    created_at: datetime
    resolved_at: datetime | None = None


class DocumentVersionSuggestionListResponse(BaseModel):
    items: list[DocumentVersionSuggestionResponse]
    total: int


class DocumentVersionsDetailResponse(BaseModel):
    document_id: UUID
    workspace_id: UUID
    profile: DocumentProfileResponse | None = None
    group: DocumentVersionGroupResponse | None = None
    pending_suggestions: list[DocumentVersionSuggestionResponse] = Field(default_factory=list)


class ConfirmVersionSuggestionRequest(BaseModel):
    relation_kind: VersionRelationKind | None = None
    newer_document_id: UUID | None = None
    group_name: str | None = Field(default=None, max_length=255)


class LinkDocumentVersionRequest(BaseModel):
    related_document_id: UUID
    direction: Literal["newer", "older"] = "newer"
    relation_kind: VersionRelationKind = "supersedes"
    group_name: str | None = Field(default=None, max_length=255)
    version_note: str | None = Field(default=None, max_length=255)


class UpdateVersionGroupRequest(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    relation_kind: VersionRelationKind | None = None
    member_order: list[UUID] | None = None


class DocumentSourceRegion(BaseModel):
    """One source region or block anchor a chunk was extracted from."""

    page_no: int | None = Field(default=None, ge=1)
    bbox: tuple[float, float, float, float] | None = None
    kind: str = "text"
    ref: str = ""
    anchor: str = ""
    text: str = ""


class ChunkRegionsResponse(BaseModel):
    regions: list[DocumentSourceRegion] = Field(default_factory=list)


class PageTextLine(BaseModel):
    text: str
    bbox: tuple[float, float, float, float]


class PageTextLayer(BaseModel):
    page_no: int
    lines: list[PageTextLine] = Field(default_factory=list)


class DocumentTextLayerResponse(BaseModel):
    pages: list[PageTextLayer] = Field(default_factory=list)


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
    title: str | None = None
    doc_category: str | None = None
    doc_subtype: str | None = None
    primary_entities: list[str] = Field(default_factory=list)
    period_label: str | None = None
    version: DocumentVersionSignalResponse | None = None


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


class AgentModelSettingsUpdate(BaseModel):
    """The chat model an admin gives the harness.

    A built-in provider owns its base URL and default model, so the admin
    names the provider and supplies a key; ``custom`` is the one choice
    that reads a typed base URL and requires an explicit model. ``api_key``
    is optional so an edit can change the endpoint or the model without
    re-typing the key; the stored key is kept when it is omitted, and is
    mandatory on the first configuration.
    """

    provider: Literal["custom", "opencode-go", "deepseek"] | None = None
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def resolve_provider(self) -> "AgentModelSettingsUpdate":
        from app.agents.providers import BUILTIN_PROVIDERS, resolve_base_url

        self.provider = self.provider or "custom"
        self.base_url = resolve_base_url(self.provider, self.base_url)
        preset = BUILTIN_PROVIDERS.get(self.provider)
        model = (self.model or "").strip() or (preset.default_model if preset else "")
        if not model:
            raise ValueError("A custom provider needs a model.")
        self.model = model
        return self


class AgentProviderInfo(BaseModel):
    key: str
    label: str
    default_model: str
    key_hint: str


class AgentModelSettingsResponse(BaseModel):
    configured: bool
    provider: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key_preview: str | None = None
    last_test_ok: bool | None = None
    last_test_model: str | None = None
    last_tested_at: datetime | None = None
    test_error: str | None = None
    updated_at: datetime | None = None
    key_hint: str | None = None
    available_providers: list[AgentProviderInfo] = Field(default_factory=list)


class AgentModelTestResult(BaseModel):
    ok: bool
    model: str | None = None
    error: str | None = None


class RetrievalSettingsUpdate(BaseModel):
    """The retrieval-stage knobs an admin can change at runtime."""

    rerank_enabled: bool
    rerank_provider: Literal["laya", "bge"] = "laya"
    rerank_model: str = Field(min_length=1, max_length=200)
    rerank_blend: float = Field(default=1.0, ge=0.0, le=1.0)
    rerank_candidates: int = Field(ge=1, le=100)
    rerank_concurrency: int = Field(ge=1, le=16)
    rerank_min_score: float | None = Field(default=None, ge=0.0, le=2.0)
    rerank_timeout_ms: int = Field(ge=100, le=60_000)


class RetrievalSettingsResponse(BaseModel):
    rerank_enabled: bool
    rerank_provider: str
    rerank_model: str
    rerank_blend: float
    rerank_candidates: int
    rerank_concurrency: int
    rerank_min_score: float | None
    rerank_timeout_ms: int
    embedding_base_url: str
    embedding_model: str
    rerank_base_url: str
    last_test_ok: bool | None = None
    last_test_ms: int | None = None
    last_test_error: str | None = None
    last_tested_at: datetime | None = None
    updated_at: datetime | None = None


class RetrievalTestResult(BaseModel):
    ok: bool
    model: str | None = None
    latency_ms: int | None = None
    score: float | None = None
    error: str | None = None


class AgentChatMessage(BaseModel):
    """One prior exchange the client replays into an ephemeral conversation."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=16_000)


class AgentChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8_000)
    history: list[AgentChatMessage] = Field(default_factory=list, max_length=30)
    session_id: str | None = Field(default=None, min_length=8, max_length=128)
    resume_context: str | None = Field(default=None, max_length=16_000)
