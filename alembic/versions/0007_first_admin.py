"""Create the one-time setup gate for the first administrator.

Existing deployments with an administrator are marked complete, so the web
setup flow is only offered when there is no administrator and the gate has
never been used.

Revision ID: 0007_first_admin
Revises: 0006_bm25_index
Create Date: 2026-11-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_first_admin"
down_revision: str | None = "0006_bm25_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE system_state (
            id integer NOT NULL,
            admin_created_at timestamp with time zone,
            CONSTRAINT pk_system_state PRIMARY KEY (id),
            CONSTRAINT ck_system_state_singleton CHECK (id = 1)
        )
        """
    )
    op.execute(
        """
        INSERT INTO system_state (id, admin_created_at)
        VALUES (
            1,
            CASE
                WHEN EXISTS (SELECT 1 FROM users WHERE role = 'admin')
                THEN now()
                ELSE NULL
            END
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS system_state")
