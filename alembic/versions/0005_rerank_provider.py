"""Add the provider and blend weight used by the optional reranker.

``rerank_provider`` defaults to the existing Laya adapter. ``rerank_blend``
defaults to 1.0, which keeps Laya's pure-rerank behaviour; the BGE cross-encoder
measured better at 0.3, where it is blended with the cosine score instead of
replacing it.

Revision ID: 0005_rerank_provider
Revises: 0004_retrieval_settings
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_rerank_provider"
down_revision: str | None = "0004_retrieval_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "retrieval_settings",
        sa.Column(
            "rerank_provider",
            sa.String(length=20),
            server_default="laya",
            nullable=False,
        ),
    )
    op.add_column(
        "retrieval_settings",
        sa.Column(
            "rerank_blend",
            sa.Float(),
            server_default="1.0",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("retrieval_settings", "rerank_blend")
    op.drop_column("retrieval_settings", "rerank_provider")
