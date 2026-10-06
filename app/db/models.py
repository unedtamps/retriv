from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")

JOB_EXTRACT = "extract"
JOB_DELETE = "delete"

RUN_QUEUED = "queued"
RUN_FETCHING = "fetching"
RUN_EXTRACTING = "extracting"
RUN_STORING_ARTIFACT = "storing_artifact"
RUN_CHUNKING = "chunking"
RUN_EMBEDDING = "embedding"
RUN_DELETING = "deleting"
RUN_READY = "ready"
RUN_RETRY_WAIT = "retry_wait"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_UNSUPPORTED = "unsupported_format"

# A claimed run is in exactly one of these. RUN_QUEUED is deliberately
# absent: an unclaimed run is not in flight, so lease recovery must not
# touch it.
ACTIVE_RUN_STATUSES: tuple[str, ...] = (
    RUN_FETCHING,
    RUN_EXTRACTING,
    RUN_STORING_ARTIFACT,
    RUN_CHUNKING,
    RUN_EMBEDDING,
    RUN_DELETING,
)
NON_TERMINAL_RUN_STATUSES: tuple[str, ...] = (
    RUN_QUEUED,
    *ACTIVE_RUN_STATUSES,
    RUN_RETRY_WAIT,
)

EMBEDDING_PENDING = "pending"
EMBEDDING_PROCESSING = "processing"
EMBEDDING_INDEXED = "indexed"
EMBEDDING_RETRY_WAIT = "retry_wait"
EMBEDDING_FAILED = "failed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(64), default="user")
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )
    auth_version: Mapped[int] = mapped_column(default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WorkspaceMembership(Base):
    __tablename__ = "workspace_memberships"
    __table_args__ = (
        CheckConstraint(
            "role IN ('owner', 'editor', 'viewer')",
            name="workspace_membership_role",
        ),
        Index("ix_workspace_memberships_workspace_user", "workspace_id", "user_id"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    granted_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Document(Base):
    """A document and, once it is enqueued, its pipeline state.

    This replaces the two tables the services used to keep in step across
    a database boundary: the API's row and the ingestion service's mirror.
    A document's status is not stored here; it is derived from the active
    processing run so the two can never disagree.

    ``created_by_user_id`` is nullable because records that arrive from an
    external system have no human uploader.
    """

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "sha256", name="uq_documents_workspace_sha256"
        ),
        Index("ix_documents_active_run", "active_processing_run_id"),
        Index("ix_documents_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True
    )
    original_filename: Mapped[str] = mapped_column(String(255))
    detected_media_type: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    bucket: Mapped[str] = mapped_column(String(255))
    object_key: Mapped[str] = mapped_column(String(1024), unique=True)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Not a foreign key: processing_runs.document_id already points back
    # here, so a constraint would make the pair circular.
    active_processing_run_id: Mapped[UUID | None] = mapped_column(nullable=True)


class ProcessingRun(Base):
    """One attempt at bringing a document to (or removing it from) READY.

    The lease and retry columns are what let the worker survive a crash:
    a run whose lease expired is reclaimed by the next worker. Every run
    carries workspace_id alongside document_id so retrieval can filter on
    one column and the composite indexes stay narrow.
    """

    __tablename__ = "processing_runs"
    __table_args__ = (
        CheckConstraint("job_kind IN ('extract', 'delete')", name="processing_run_kind"),
        UniqueConstraint(
            "id", "workspace_id", "document_id", name="uq_processing_runs_identity"
        ),
        Index("ix_processing_runs_dispatch", "status", "available_at"),
        Index("ix_processing_runs_lease", "status", "lease_until"),
        Index("ix_processing_runs_document", "document_id"),
        Index("ix_processing_runs_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    job_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=RUN_QUEUED, server_default=RUN_QUEUED, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_token: Mapped[UUID | None] = mapped_column(nullable=True)

    # Where this attempt's extracted artifact landed, so a failed or
    # superseded run can clean up after itself and a delete can find it.
    artifact_bucket: Mapped[str | None] = mapped_column(String(255), nullable=True)
    artifact_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    manifest_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # Progress counters drive the workspace-scoped status queries the UI
    # polls, so they live with the run rather than on the document.
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    embedded_chunk_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    # A CSV run's progress and provenance. csv_columns labels each returned
    # cell, so it lives on the run rather than on all 100k rows.
    row_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    csv_columns: Mapped[list[str] | None] = mapped_column(JSON_TYPE, nullable=True)
    embedding_profile: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("processing_run_id", "ordinal", name="uq_document_chunks_run_ordinal"),
        Index("ix_document_chunks_document", "workspace_id", "document_id"),
        Index("ix_document_chunks_active_lookup", "workspace_id", "processing_run_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    processing_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("processing_runs.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    section_path: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    page_numbers: Mapped[list[int]] = mapped_column(JSON_TYPE, default=list)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DocumentRow(Base):
    """One CSV row, stored for SQL rather than for search.

    Rows deliberately have no foreign key into ``document_chunks``. A CSV is
    all numeric or identifier columns, so embedding each row buys noise at
    16KB of vector storage per row and finds nothing a filter would not;
    the rows are the queryable form and Qdrant stays empty for these
    documents by design.

    The id is derived from ``(run_id, row_number)`` so it is stable across a
    re-upload of identical content, and it doubles as the ``chunk_id`` the
    agent's citation plumbing expects. Nothing needs it to resolve inside
    ``document_chunks``: the citation gate checks label membership in the
    turn's own source set, which this row is registered into.
    """

    __tablename__ = "document_rows"
    __table_args__ = (
        UniqueConstraint(
            "processing_run_id", "row_number", name="uq_document_rows_run_row"
        ),
        Index("ix_document_rows_active_lookup", "workspace_id", "processing_run_id"),
        # The model writes filter predicates into JSONB keys, so a GIN index
        # is what makes `data->>'status' = 'OPEN'` an index lookup rather than
        # a scan across every workspace's rows.
        Index("ix_document_rows_data", "data", postgresql_using="gin"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    processing_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("processing_runs.id", ondelete="CASCADE"), nullable=False
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ChunkEmbedding(Base):
    __tablename__ = "chunk_embeddings"
    __table_args__ = (
        UniqueConstraint("qdrant_point_id", name="uq_chunk_embeddings_qdrant_point_id"),
        Index(
            "ix_chunk_embeddings_dispatch",
            "processing_run_id",
            "embedding_profile",
            "status",
        ),
        Index(
            "ix_chunk_embeddings_document",
            "workspace_id",
            "document_id",
            "embedding_profile",
        ),
    )

    chunk_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_chunks.id", ondelete="CASCADE"), primary_key=True
    )
    embedding_profile: Mapped[str] = mapped_column(String(255), primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    processing_run_id: Mapped[UUID] = mapped_column(nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    qdrant_point_id: Mapped[UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=EMBEDDING_PENDING, server_default=EMBEDDING_PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AgentConversation(Base):
    __tablename__ = "agent_conversations"
    __table_args__ = (
        Index("ix_agent_conversations_owner_updated", "owner_user_id", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(160), nullable=True)
    next_sequence: Mapped[int] = mapped_column(default=1, server_default="1")
    active_turn_id: Mapped[UUID | None] = mapped_column(nullable=True)
    active_turn_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AgentConversationTurn(Base):
    __tablename__ = "agent_conversation_turns"
    __table_args__ = (
        CheckConstraint(
            "status IN ('processing', 'completed', 'failed')",
            name="agent_conversation_turn_status",
        ),
        UniqueConstraint(
            "conversation_id", "sequence", name="uq_agent_turns_conversation_sequence"
        ),
        UniqueConstraint(
            "conversation_id", "request_id", name="uq_agent_turns_conversation_request"
        ),
        Index("ix_agent_turns_conversation_sequence", "conversation_id", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    reservation_token: Mapped[UUID] = mapped_column(nullable=False, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), nullable=False
    )
    request_id: Mapped[UUID] = mapped_column(nullable=False)
    sequence: Mapped[int] = mapped_column(nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_TYPE, default=list, server_default="[]", nullable=False
    )
    source_workspace_ids: Mapped[list[str]] = mapped_column(
        JSON_TYPE, default=list, server_default="[]", nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default="processing", server_default="processing", nullable=False
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
