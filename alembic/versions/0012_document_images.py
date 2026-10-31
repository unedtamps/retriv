"""Add document_images: the address book for extracted pictures.

The worker ships every curated picture to the artifact bucket as one object
-- a render bounded to the edge a vision model reads at, which is everything
a model can use since it downscales past that edge itself. This table is the
ref-keyed map between the picture markers a chunk text carries and the
stored object: key, media type, checksum, page, caption, and the words the
vision stage said about it. Both document links cascade, so a deleted
document takes its picture rows with it.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_document_images"
down_revision: str | None = "0011_rename_workspace_origin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "document_images",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("origin_workspace_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "document_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "processing_run_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("processing_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ref", sa.String(length=100), nullable=False),
        sa.Column("bucket", sa.String(length=255), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("media_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("describe_hint", sa.String(length=20), nullable=False),
        sa.Column("caption", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("described", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("document_id", "ref", name="uq_document_images_document_ref"),
    )
    op.create_index(
        "ix_document_images_active_lookup",
        "document_images",
        ["origin_workspace_id", "document_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_document_images_active_lookup", table_name="document_images")
    op.drop_table("document_images")
