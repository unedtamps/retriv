"""Rotating refresh tokens, so a session outlives one access token.

Before this table the only session credential was the access token, valid for
``RETRIV_ACCESS_TOKEN_MINUTES`` and impossible to renew: an open tab hit the
login screen on the hour. A refresh token fixes that, but only if it is stored
the way a password is -- as a hash, never as the value itself.

``family_id`` is what makes rotation safe. Every use mints a replacement and
revokes the presented token; if a revoked token is presented again, the whole
chain is revoked in one query, because the only way an old link resurfaces is
that a copy of it leaked.

``auth_version`` snapshots the user's value at mint time so a password reset
invalidates chains that were issued before it, without touching rows that were
issued after.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_refresh_tokens"
down_revision: str | None = "0013_single_active_extract"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("auth_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_family_id", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
