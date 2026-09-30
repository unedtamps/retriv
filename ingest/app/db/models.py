from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKeyConstraint,
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

ACTIVE_RUN_STATUSES = (
    RUN_FETCHING,
    RUN_EXTRACTING,
    RUN_STORING_ARTIFACT,
    RUN_CHUNKING,
    RUN_EMBEDDING,
    RUN_DELETING,
)
NON_TERMINAL_RUN_STATUSES = (RUN_QUEUED, *ACTIVE_RUN_STATUSES, RUN_RETRY_WAIT)

EMBEDDING_PENDING = "pending"
EMBEDDING_PROCESSING = "processing"
EMBEDDING_INDEXED = "indexed"
EMBEDDING_RETRY_WAIT = "retry_wait"
EMBEDDING_FAILED = "failed"


class EventInbox(Base):
    __tablename__ = "event_inbox"

    event_id: Mapped[UUID] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SourceDocument(Base):
    __tablename__ = "source_documents"
    __table_args__ = (
        UniqueConstraint("source_event_id", name="uq_source_documents_source_event_id"),
        Index("ix_source_documents_active_run", "active_processing_run_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(primary_key=True)
    document_id: Mapped[UUID] = mapped_column(primary_key=True)
    source_event_id: Mapped[UUID] = mapped_column(nullable=False)
    source_occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_bucket: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_object_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    active_processing_run_id: Mapped[UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ProcessingRun(Base):
    __tablename__ = "processing_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "document_id"],
            ["source_documents.workspace_id", "source_documents.document_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "id", "workspace_id", "document_id", name="uq_processing_runs_identity"
        ),
        Index("ix_processing_runs_dispatch", "status", "available_at"),
        Index("ix_processing_runs_lease", "status", "lease_until"),
        Index("ix_processing_runs_document", "workspace_id", "document_id"),
        Index("ix_processing_runs_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
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
    artifact_bucket: Mapped[str | None] = mapped_column(String(255), nullable=True)
    artifact_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    manifest_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    embedded_chunk_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
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
        ForeignKeyConstraint(
            ["processing_run_id", "workspace_id", "document_id"],
            [
                "processing_runs.id",
                "processing_runs.workspace_id",
                "processing_runs.document_id",
            ],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "processing_run_id", "ordinal", name="uq_document_chunks_run_ordinal"
        ),
        Index("ix_document_chunks_document", "workspace_id", "document_id"),
        Index("ix_document_chunks_active_lookup", "workspace_id", "processing_run_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    processing_run_id: Mapped[UUID] = mapped_column(nullable=False)
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


class ChunkEmbedding(Base):
    __tablename__ = "chunk_embeddings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["chunk_id"],
            ["document_chunks.id"],
            ondelete="CASCADE",
        ),
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

    chunk_id: Mapped[UUID] = mapped_column(primary_key=True)
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
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
