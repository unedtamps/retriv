"""Add durable document deletion state.

Revision ID: 0002_document_status
Revises: 0001_initial
Create Date: 2026-09-20
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_document_status"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("status", sa.String(length=32), server_default="ready", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("documents", "status")
