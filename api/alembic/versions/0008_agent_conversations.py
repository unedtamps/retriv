"""Add private agent conversations and source references.

Revision ID: 0008_agent_conversations
Revises: 0007_workspace_authorization
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_agent_conversations"
down_revision: str | None = "0007_workspace_authorization"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    source_refs_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "agent_conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=True),
        sa.Column("next_sequence", sa.Integer(), server_default="1", nullable=False),
        sa.Column("active_turn_id", sa.Uuid(), nullable=True),
        sa.Column("active_turn_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_agent_conversations_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_conversations")),
    )
    op.create_index(
        "ix_agent_conversations_owner_updated",
        "agent_conversations",
        ["owner_user_id", "updated_at"],
        unique=False,
    )

    op.create_table(
        "agent_conversation_turns",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reservation_token", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("source_refs", source_refs_type, server_default=sa.text("'[]'"), nullable=False),
        sa.Column(
            "source_workspace_ids",
            source_refs_type,
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), server_default="processing", nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('processing', 'completed', 'failed')",
            name=op.f("ck_agent_conversation_turns_agent_conversation_turn_status"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["agent_conversations.id"],
            name=op.f("fk_agent_conversation_turns_conversation_id_agent_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_conversation_turns")),
        sa.UniqueConstraint(
            "conversation_id", "request_id", name="uq_agent_turns_conversation_request"
        ),
        sa.UniqueConstraint(
            "conversation_id", "sequence", name="uq_agent_turns_conversation_sequence"
        ),
    )
    op.create_index(
        "ix_agent_turns_conversation_sequence",
        "agent_conversation_turns",
        ["conversation_id", "sequence"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_turns_conversation_sequence", table_name="agent_conversation_turns"
    )
    op.drop_table("agent_conversation_turns")
    op.drop_index(
        "ix_agent_conversations_owner_updated", table_name="agent_conversations"
    )
    op.drop_table("agent_conversations")
