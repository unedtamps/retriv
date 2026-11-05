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
JOB_ACCESS = "access"

RUN_QUEUED = "queued"
RUN_FETCHING = "fetching"
RUN_AWAITING_PARSER = "awaiting_parser"
RUN_EXTRACTING = "extracting"
RUN_DESCRIBING = "describing"
RUN_STORING_ARTIFACT = "storing_artifact"
RUN_CHUNKING = "chunking"
RUN_EMBEDDING = "embedding"
RUN_SYNCING = "syncing"
RUN_DELETING = "deleting"
RUN_READY = "ready"
RUN_RETRY_WAIT = "retry_wait"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_UNSUPPORTED = "unsupported_format"

ACTIVE_RUN_STATUSES: tuple[str, ...] = (
    RUN_FETCHING,
    RUN_AWAITING_PARSER,
    RUN_EXTRACTING,
    RUN_DESCRIBING,
    RUN_STORING_ARTIFACT,
    RUN_CHUNKING,
    RUN_EMBEDDING,
    RUN_SYNCING,
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
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    auth_version: Mapped[int] = mapped_column(default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
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
    """A canonical document and, once it is enqueued, its pipeline state.

    A document is identified by its content hash and is processed once, no
    matter how many workspaces link to it. ``origin_workspace_id`` says where
    the raw object and artifacts live, not who may read the document. Access
    is the set of rows in ``DocumentWorkspace``.

    This replaces the two tables the services used to keep in step across
    a database boundary: the API's row and the ingestion service's mirror.
    A document's status is not stored here; it is derived from the active
    processing run so the two can never disagree.

    ``created_by_user_id`` is nullable because records that arrive from an
    external system have no human uploader.
    """

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("sha256", name="uq_documents_sha256"),
        Index("ix_documents_active_run", "active_processing_run_id"),
        Index(
            "ix_documents_origin_workspace_created",
            "origin_workspace_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    origin_workspace_id: Mapped[UUID] = mapped_column(
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active_processing_run_id: Mapped[UUID | None] = mapped_column(nullable=True)


class DocumentWorkspace(Base):
    """One workspace's view of a canonical document.

    The filename is per link on purpose: the same bytes may be uploaded as
    ``report.pdf`` in Finance and ``laporan.pdf`` in Legal. Deleting a link
    only removes this workspace's access; the canonical document and its
    vectors are removed when the last link goes away.
    """

    __tablename__ = "document_workspaces"
    __table_args__ = (
        Index(
            "ix_document_workspaces_workspace_created",
            "workspace_id",
            "created_at",
        ),
        Index("ix_document_workspaces_document", "document_id"),
    )

    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    filename: Mapped[str] = mapped_column(String(255))
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProcessingRun(Base):
    """One attempt at bringing a document to (or removing it from) READY.

    The lease and retry columns are what let the worker survive a crash:
    a run whose lease expired is reclaimed by the next worker.
    ``origin_workspace_id`` is where the document's artifacts live; it is not
    the set of workspaces that may read the document.
    """

    __tablename__ = "processing_runs"
    __table_args__ = (
        CheckConstraint(
            "job_kind IN ('extract', 'delete', 'access')",
            name="processing_run_kind",
        ),
        UniqueConstraint(
            "id",
            "origin_workspace_id",
            "document_id",
            name="uq_processing_runs_identity",
        ),
        Index("ix_processing_runs_dispatch", "status", "available_at"),
        Index("ix_processing_runs_lease", "status", "lease_until"),
        Index("ix_processing_runs_document", "document_id"),
        Index(
            "ix_processing_runs_origin_workspace_created",
            "origin_workspace_id",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    origin_workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    job_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=RUN_QUEUED, server_default=RUN_QUEUED, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_token: Mapped[UUID | None] = mapped_column(nullable=True)

    artifact_bucket: Mapped[str | None] = mapped_column(String(255), nullable=True)
    artifact_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    manifest_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    chunk_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    embedded_chunk_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    row_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    csv_columns: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON_TYPE, nullable=True)
    embedding_profile: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint("processing_run_id", "ordinal", name="uq_document_chunks_run_ordinal"),
        Index("ix_document_chunks_document", "origin_workspace_id", "document_id"),
        Index(
            "ix_document_chunks_active_lookup",
            "origin_workspace_id",
            "processing_run_id",
        ),
        Index(
            "ix_document_chunks_section_key",
            "processing_run_id",
            "section_key",
            postgresql_ops={"section_key": "text_pattern_ops"},
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    origin_workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    processing_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("processing_runs.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    section_path: Mapped[list[str]] = mapped_column(JSON_TYPE, default=list)
    section_key: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_heading_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    page_numbers: Mapped[list[int]] = mapped_column(JSON_TYPE, default=list)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DocumentImage(Base):
    """One extracted picture, stored for the agent to look at.

    The bytes live in the artifact bucket under the run's own attempt
    prefix, and this row is the only address anyone outside the worker
    holds: ref to keys, media type, checksum, the page it sat on, and the
    words the vision stage said about it. Rows deliberately have no
    foreign key into ``document_chunks`` -- a picture's description is
    embedded inside its chunk's text, and several chunks may reference
    one picture -- so the ref is the join key, exactly as it is in the
    chunk text itself.

    Access is checked in application code by joining the document's
    workspace links, like the MCP download tool does; the SQL agent path
    does not read this table, so no row-level policy is attached here.
    """

    __tablename__ = "document_images"
    __table_args__ = (
        UniqueConstraint("document_id", "ref", name="uq_document_images_document_ref"),
        Index(
            "ix_document_images_active_lookup",
            "origin_workspace_id",
            "document_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    origin_workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    document_id: Mapped[UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    processing_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("processing_runs.id", ondelete="CASCADE"), nullable=False
    )
    ref: Mapped[str] = mapped_column(String(100), nullable=False)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    media_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    describe_hint: Mapped[str] = mapped_column(String(20), nullable=False, default="picture")
    caption: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    described: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DocumentRow(Base):
    """One row of a tabular document, stored for SQL rather than for search.

    A CSV is one sheet with no name; a workbook is several named ones. The
    ``sheet`` column carries that name, empty for a CSV, so both formats
    land in this table with one query surface: the agent filters on
    ``document_id`` and, for a workbook, on ``sheet``. Rows deliberately
    have no foreign key into ``document_chunks``. A tabular document is all
    numeric or identifier columns, so embedding each row buys noise at
    16KB of vector storage per row and finds nothing a filter would not;
    the rows are the queryable form and no vectors are stored for these
    documents by design.

    The id is derived from ``(run_id, sheet, row_number)`` -- a CSV keeps
    the older ``(run_id, row_number)`` seed so existing rows keep their
    identity -- and it doubles as the ``chunk_id`` the agent's citation
    plumbing expects. Nothing needs it to resolve inside
    ``document_chunks``: the citation gate checks label membership in the
    turn's own source set, which this row is registered into.
    """

    __tablename__ = "document_rows"
    __table_args__ = (
        UniqueConstraint(
            "processing_run_id",
            "sheet",
            "row_number",
            name="uq_document_rows_run_sheet_row",
        ),
        Index(
            "ix_document_rows_active_lookup",
            "origin_workspace_id",
            "processing_run_id",
        ),
        Index("ix_document_rows_document_sheet", "document_id", "sheet"),
        Index("ix_document_rows_data", "data", postgresql_using="gin"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    origin_workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    processing_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("processing_runs.id", ondelete="CASCADE"), nullable=False
    )
    sheet: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ChunkEmbedding(Base):
    """The embedding ledger, and since 0018 the vector store itself.

    The ``embedding`` column is deliberately not mapped here: it is a
    pgvector ``vector(2000)`` that only exists in Postgres, and this
    metadata drives the SQLite test schema too. Every vector read and
    write goes through raw SQL that CASTs a text literal -- the worker's
    batch update in processing, and the search in retrieval.context.
    """

    __tablename__ = "chunk_embeddings"
    __table_args__ = (
        Index(
            "ix_chunk_embeddings_dispatch",
            "processing_run_id",
            "embedding_profile",
            "status",
        ),
        Index(
            "ix_chunk_embeddings_document",
            "origin_workspace_id",
            "document_id",
            "embedding_profile",
        ),
    )

    chunk_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_chunks.id", ondelete="CASCADE"), primary_key=True
    )
    embedding_profile: Mapped[str] = mapped_column(String(255), primary_key=True)
    origin_workspace_id: Mapped[UUID] = mapped_column(nullable=False)
    document_id: Mapped[UUID] = mapped_column(nullable=False)
    processing_run_id: Mapped[UUID] = mapped_column(nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=EMBEDDING_PENDING, server_default=EMBEDDING_PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ApiKey(Base):
    """A long-lived bearer credential for one user.

    Only ``key_hash`` is stored: the plaintext is shown once at creation and
    cannot be recovered. Lookup goes through the SHA-256 hash, so there is no
    column to index that an attacker with read access could use directly --
    the hash is the credential, and it is what gets rate-checked on use.

    The key carries no workspace scope of its own. Scope is derived from the
    owning user's live memberships on every request, so removing a member
    takes effect immediately instead of waiting for a key to expire.
    """

    __tablename__ = "api_keys"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RefreshToken(Base):
    """One link in a rotating chain of refresh tokens for one user.

    Like ``ApiKey``, only the SHA-256 hash is stored: the plaintext exists
    once, in the response that minted it, and cannot be recovered from the
    database.

    Rotation replaces a token with a new one on every use. ``family_id`` is
    the id of the token the chain started from, so a token being presented a
    second time is one indexed lookup away from revoking the whole chain --
    that reuse is the signature of a stolen token, and the OAuth security BCP
    says the response is to end the session, not to guess which holder is the
    attacker.

    ``auth_version`` snapshots the user's value at mint time, so a password
    reset closes every chain, not just the access token that happened to be
    in flight.
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    family_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    auth_version: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentModelSetting(Base):
    """The one chat model the in-server agent harness uses, chosen by an admin.

    A singleton: the primary key is pinned to 1, so a second row cannot
    appear and every reader selects the same row. The API key is stored as
    it was typed -- the database is the only copy -- and is never served
    back; responses carry its last four characters so an admin can tell
    which key is configured without seeing it.
    """

    __tablename__ = "agent_model_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="agent_model_settings_singleton"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    provider: Mapped[str] = mapped_column(
        String(32), default="custom", server_default="custom", nullable=False
    )
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key: Mapped[str] = mapped_column(String(500), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
