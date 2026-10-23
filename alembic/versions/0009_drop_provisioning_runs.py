"""Drop the provisioning ledger.

Provisioning used to be a one-shot CLI fed by an S3 manifest. The first
administrator now comes from ``RETRIV_BOOTSTRAP_ADMIN_*`` on API startup, so
there is no manifest, no migration id, and nothing to record. The table is
dropped rather than left behind: an empty table with a name like this reads
like a live subsystem, and nothing references it any more.

The down direction rebuilds an empty table. The history it held is gone, which
is the point: this is not a reversible feature.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009_drop_provisioning_runs"
down_revision: str | None = "0008_section_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("provisioning_runs")


def downgrade() -> None:
    op.create_table(
        "provisioning_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("migration_id", sa.String(length=255), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_uri", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "result",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provisioning_runs")),
    )
    op.create_index(
        op.f("ix_provisioning_runs_migration_id"),
        "provisioning_runs",
        ["migration_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_provisioning_runs_created_at"),
        "provisioning_runs",
        ["created_at"],
    )
