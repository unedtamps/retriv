"""Create the document context processing schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "event_inbox",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_event_inbox")),
    )
    op.create_table(
        "source_documents",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("source_event_id", sa.Uuid(), nullable=False),
        sa.Column("source_occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=True),
        sa.Column("media_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("raw_bucket", sa.String(length=255), nullable=True),
        sa.Column("raw_object_key", sa.String(length=1024), nullable=True),
        sa.Column("active_processing_run_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "workspace_id", "document_id", name=op.f("pk_source_documents")
        ),
        sa.UniqueConstraint(
            "source_event_id", name="uq_source_documents_source_event_id"
        ),
    )
    op.create_index(
        "ix_source_documents_active_run",
        "source_documents",
        ["active_processing_run_id"],
        unique=False,
    )
    op.create_table(
        "processing_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("job_kind", sa.String(length=32), nullable=False),
        sa.Column("pipeline_version", sa.String(length=100), nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default="queued", nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("artifact_bucket", sa.String(length=255), nullable=True),
        sa.Column("artifact_key", sa.String(length=1024), nullable=True),
        sa.Column("manifest_key", sa.String(length=1024), nullable=True),
        sa.Column("chunk_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["workspace_id", "document_id"],
            ["source_documents.workspace_id", "source_documents.document_id"],
            name=op.f("fk_processing_runs_workspace_id_source_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processing_runs")),
        sa.UniqueConstraint(
            "id", "workspace_id", "document_id", name="uq_processing_runs_identity"
        ),
    )
    op.create_index(
        "ix_processing_runs_dispatch",
        "processing_runs",
        ["status", "available_at"],
        unique=False,
    )
    op.create_index(
        "ix_processing_runs_document",
        "processing_runs",
        ["workspace_id", "document_id"],
        unique=False,
    )
    op.create_index(
        "ix_processing_runs_lease",
        "processing_runs",
        ["status", "lease_until"],
        unique=False,
    )
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("processing_run_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column(
            "section_path", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "page_numbers", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["processing_run_id", "workspace_id", "document_id"],
            [
                "processing_runs.id",
                "processing_runs.workspace_id",
                "processing_runs.document_id",
            ],
            name=op.f("fk_document_chunks_processing_run_id_processing_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_chunks")),
        sa.UniqueConstraint(
            "processing_run_id", "ordinal", name="uq_document_chunks_run_ordinal"
        ),
    )
    op.create_index(
        "ix_document_chunks_active_lookup",
        "document_chunks",
        ["workspace_id", "processing_run_id"],
        unique=False,
    )
    op.create_index(
        "ix_document_chunks_document",
        "document_chunks",
        ["workspace_id", "document_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("document_chunks")
    op.drop_table("processing_runs")
    op.drop_table("source_documents")
    op.drop_table("event_inbox")
