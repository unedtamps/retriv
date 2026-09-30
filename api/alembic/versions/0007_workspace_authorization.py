"""Add session invalidation and workspace memberships.

Revision ID: 0007_workspace_authorization
Revises: 0006_add_outbox_lease_token
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_workspace_authorization"
down_revision: str | None = "0006_add_outbox_lease_token"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("auth_version", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_table(
        "workspace_memberships",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("granted_by_user_id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint(
            "role IN ('owner', 'editor', 'viewer')",
            name="workspace_membership_role",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_workspace_memberships_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_workspace_memberships_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_user_id"],
            ["users.id"],
            name=op.f("fk_workspace_memberships_granted_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("user_id", "workspace_id", name=op.f("pk_workspace_memberships")),
    )
    op.create_index(
        "ix_workspace_memberships_workspace_user",
        "workspace_memberships",
        ["workspace_id", "user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workspace_memberships_workspace_user", table_name="workspace_memberships"
    )
    op.drop_table("workspace_memberships")
    op.drop_column("users", "auth_version")
