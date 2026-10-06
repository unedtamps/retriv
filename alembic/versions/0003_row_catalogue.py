"""Row-level security on processing_runs, and read access for the catalogue.

The row-query tool needs to tell the model which CSV files exist and what
columns each one has, because column names differ per file and Postgres has
no schema to infer them from. That answer lives in
``processing_runs.csv_columns``, which means the reader role needs access to
the table.

Access alone is not acceptable. ``processing_runs`` carries ``lease_token``
and ``last_error``, and its ``workspace_id`` means a policy is available to
keep the reader inside the scope it was given -- exactly as already applied
to ``document_rows`` and ``documents``.

The GRANT goes to the role but the table is *not* added to the model's
allowlist, so the model still cannot name it in a query. It does not need
to know a lease token or an embedding profile name, and leaving it readable
would widen the blast radius of a generated statement for nothing.

Separate from 0002 because that revision is already applied to running
environments, and altering a table's security in place would not replay for
them.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_row_catalogue"
down_revision: str | None = "0002_csv_rows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

READER_ROLE = "retriv_reader"
SCOPE_SETTING = "retriv.workspace_scope"


def upgrade() -> None:
    op.execute("ALTER TABLE processing_runs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE processing_runs FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY runs_in_scope ON processing_runs "  # noqa: S608
        f"USING (workspace_id = ANY (current_setting('{SCOPE_SETTING}', true)::uuid[]))"
    )
    # Optional role, same as 0002: it is created operationally so its password
    # never lands in a migration.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{READER_ROLE}') THEN
                GRANT SELECT ON processing_runs TO {READER_ROLE};
            END IF;
        END $$
        """  # noqa: S608 - interpolates READER_ROLE only, never input
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS runs_in_scope ON processing_runs")
    op.execute("ALTER TABLE processing_runs NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE processing_runs DISABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{READER_ROLE}') THEN
                REVOKE SELECT ON processing_runs FROM {READER_ROLE};
            END IF;
        END $$
        """  # noqa: S608 - interpolates READER_ROLE only, never input
    )