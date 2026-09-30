"""Add idempotent provisioning run records.

Revision ID: 0003_provisioning_runs
Revises: 0002_document_status
Create Date: 2026-09-20
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_provisioning_runs"
down_revision: str | None = "0002_document_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provisioning_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("migration_id", sa.String(length=255), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_uri", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provisioning_runs")),
    )
    op.create_index(
        op.f("ix_provisioning_runs_migration_id"),
        "provisioning_runs",
        ["migration_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_provisioning_runs_created_at"),
        "provisioning_runs",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("provisioning_runs")
