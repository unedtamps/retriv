"""Track Qdrant embedding state for document chunks.

Revision ID: 0002_qdrant_embeddings
Revises: 0001_initial
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_qdrant_embeddings"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "processing_runs",
        sa.Column(
            "embedded_chunk_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "processing_runs",
        sa.Column("embedding_profile", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "processing_runs",
        sa.Column("embedding_model", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "processing_runs",
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
    )
    op.create_table(
        "chunk_embeddings",
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("embedding_profile", sa.String(length=255), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("processing_run_id", sa.Uuid(), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("qdrant_point_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["document_chunks.id"],
            name=op.f("fk_chunk_embeddings_chunk_id_document_chunks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "chunk_id", "embedding_profile", name=op.f("pk_chunk_embeddings")
        ),
        sa.UniqueConstraint(
            "qdrant_point_id", name="uq_chunk_embeddings_qdrant_point_id"
        ),
    )
    op.create_index(
        "ix_chunk_embeddings_dispatch",
        "chunk_embeddings",
        ["processing_run_id", "embedding_profile", "status"],
        unique=False,
    )
    op.create_index(
        "ix_chunk_embeddings_document",
        "chunk_embeddings",
        ["workspace_id", "document_id", "embedding_profile"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("chunk_embeddings")
    op.drop_column("processing_runs", "embedding_dimensions")
    op.drop_column("processing_runs", "embedding_model")
    op.drop_column("processing_runs", "embedding_profile")
    op.drop_column("processing_runs", "embedded_chunk_count")
