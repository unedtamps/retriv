"""Index processing-run feeds by workspace and creation time.

Revision ID: 0003_proc_run_workspace_created
Revises: 0002_qdrant_embeddings
Create Date: 2026-09-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_proc_run_workspace_created"
down_revision: str | None = "0002_qdrant_embeddings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_processing_runs_workspace_created",
        "processing_runs",
        ["workspace_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_processing_runs_workspace_created", table_name="processing_runs"
    )
