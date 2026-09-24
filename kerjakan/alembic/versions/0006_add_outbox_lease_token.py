"""Add an outbox lease fencing token.

Revision ID: 0006_add_outbox_lease_token
Revises: 0005_add_outbox_events
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_add_outbox_lease_token"
down_revision: str | None = "0005_add_outbox_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "outbox_events",
        sa.Column("lease_token", sa.Uuid(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("outbox_events", "lease_token")
