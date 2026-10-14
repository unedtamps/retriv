"""Drop the chat feature's tables.

The in-app agent is gone. What it did -- semantic search and CSV row queries
-- is now offered to external clients as MCP tools under an API key, and the
dashboard has no chat pane left that would read these tables. Nothing in the
application references them any more, so they are dropped rather than left
behind as tables that look live.

The down direction rebuilds an empty schema. The conversation history it held
is not coming back, which is the point: this is not a reversible feature.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_drop_agent_conversations"
down_revision: str | None = "0004_api_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("agent_conversation_turns")
    op.drop_table("agent_conversations")


def downgrade() -> None:
    op.create_table(
        "agent_conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=True),
        sa.Column("next_sequence", sa.Integer(), server_default="1", nullable=False),
        sa.Column("active_turn_id", sa.Uuid(), nullable=True),
        sa.Column("active_turn_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
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
        op.f("ix_agent_conversations_owner_updated"),
        "agent_conversations",
        ["owner_user_id", "updated_at"],
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
        sa.Column(
            "source_refs",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "source_workspace_ids",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), server_default="processing", nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
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
        op.f("ix_agent_turns_conversation_sequence"),
        "agent_conversation_turns",
        ["conversation_id", "sequence"],
    )
