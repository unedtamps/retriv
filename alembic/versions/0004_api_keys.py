"""API keys: a long-lived bearer credential for one user.

The MCP server needs an authenticator that is not a short-lived JWT: MCP
clients (Claude Code, Codex, custom agents) hold one header credential for
the life of a session and cannot refresh a token mid-session.

The table stores only a SHA-256 hash of the key plus a short display
prefix. The plaintext is shown once at creation, exactly like a password:
nothing on disk can be turned back into a usable credential, and revoking
is a single UPDATE on a row the attacker cannot locate by value.

No scope columns. Workspace access is derived from the owning user's live
memberships on every request, so demoting or removing the user revokes the
key's reach immediately rather than when a snapshot expires.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_api_keys"
down_revision: str | None = "0003_row_catalogue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("key_prefix", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])
    op.create_index("ix_api_keys_key_prefix", "api_keys", ["key_prefix"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_key_prefix", table_name="api_keys")
    op.drop_index("ix_api_keys_user_id", table_name="api_keys")
    op.drop_table("api_keys")
