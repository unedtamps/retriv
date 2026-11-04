"""The agent model setting names the provider preset it came from.

A preset owns the base URL -- the admin picks OpenCode Go or DeepSeek and
supplies a key, and the server knows the address. The column records which
choice was made; ``custom`` keeps the typed-URL behavior it replaces.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_agent_model_provider"
down_revision: str | None = "0016_agent_model_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_model_settings",
        sa.Column(
            "provider",
            sa.String(32),
            nullable=False,
            server_default="custom",
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_model_settings", "provider")
