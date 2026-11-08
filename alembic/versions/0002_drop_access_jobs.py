"""Drop the no-op access processing job and its legacy rows.

Revision ID: 0002_drop_access_jobs
Revises: 0001_baseline
Create Date: 2026-11-09
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_drop_access_jobs"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_processing_runs_processing_run_kind"


def upgrade() -> None:
    op.execute("DELETE FROM processing_runs WHERE job_kind = 'access'")
    op.execute(f"ALTER TABLE processing_runs DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE processing_runs ADD CONSTRAINT {_CONSTRAINT} "
        "CHECK (job_kind IN ('extract', 'delete'))"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE processing_runs DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE processing_runs ADD CONSTRAINT {_CONSTRAINT} "
        "CHECK (job_kind IN ('extract', 'delete', 'access'))"
    )
