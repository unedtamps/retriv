"""Remove database-backed audit event storage.

Revision ID: 0004_remove_audit_events
Revises: 0003_provisioning_runs
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_remove_audit_events"
down_revision: str | None = "0003_provisioning_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("audit_events")


def downgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("result", sa.String(length=50), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(op.f("ix_audit_events_action"), "audit_events", ["action"], unique=False)
    op.create_index(
        op.f("ix_audit_events_actor_user_id"),
        "audit_events",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_audit_events_created_at"), "audit_events", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_audit_events_document_id"), "audit_events", ["document_id"], unique=False
    )
    op.create_index(
        op.f("ix_audit_events_workspace_id"), "audit_events", ["workspace_id"], unique=False
    )
