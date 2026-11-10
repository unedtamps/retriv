"""Add the singleton retrieval settings used by the optional Laya reranker.

Reranking is off by default; the row only exists once an admin saves the
retrieval settings. Keeping it separate from ``agent_model_settings`` means
the chat model and the retrieval stage can be changed independently.

Revision ID: 0004_retrieval_settings
Revises: 0003_drop_unused_indexes
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_retrieval_settings"
down_revision: str | None = "0003_drop_unused_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "retrieval_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "rerank_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "rerank_model",
            sa.String(length=200),
            server_default="laya",
            nullable=False,
        ),
        sa.Column(
            "rerank_candidates",
            sa.Integer(),
            server_default="30",
            nullable=False,
        ),
        sa.Column(
            "rerank_concurrency",
            sa.Integer(),
            server_default="8",
            nullable=False,
        ),
        sa.Column("rerank_min_score", sa.Float(), nullable=True),
        sa.Column(
            "rerank_timeout_ms",
            sa.Integer(),
            server_default="10000",
            nullable=False,
        ),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.Column("last_test_ms", sa.Integer(), nullable=True),
        sa.Column("last_test_error", sa.String(length=500), nullable=True),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id = 1", name="retrieval_settings_singleton"),
    )


def downgrade() -> None:
    op.drop_table("retrieval_settings")
