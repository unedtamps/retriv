"""CSV documents, stored as rows and queried with SQL.

Replaces the XLSX upload path. Spreadsheets were accepted by the validator
and then rejected by the extractor, so an accepted upload was guaranteed to
fail; CSV is plain text and needs none of the archive hardening OOXML did.

What this migration adds, and why the shape is what it is:

- ``document_rows`` holds one row per CSV line with its cells in ``data``.
  There is no foreign key into ``document_chunks`` and no embedding ledger
  entry: the columns these files carry are numeric and identifier values,
  where a vector is indistinguishable noise. Rows are the queryable form.
- ``processing_runs.row_count`` and ``csv_columns`` are the run's progress
  and its header. The header is needed when a query result is rendered back
  to the model, and storing it once beats repeating it on 100k rows.
- Row Level Security is enabled and **forced** on ``document_rows``. The
  agent writes SQL, and the isolation is a database property rather than a
  predicate the generated statement is trusted to carry.

  FORCE only binds the table's own owner; a superuser or a role carrying
  BYPASSRLS ignores the policy either way. The application role is currently
  a superuser, so FORCE is inert in this deployment and the policy is doing
  its work only for the non-superuser reader role the SQL tool connects as.
  It is kept because it is what protects the table if the application role is
  ever reduced to a normal owner, which is the natural hardening step.

  ``current_setting`` is read with ``missing_ok``. Without it the expression
  raises when the setting is absent, and RLS applies that same expression to
  INSERT as a WITH CHECK -- so a connection that writes rows without setting
  the scope fails with an error about a missing setting rather than an
  isolation problem. With ``missing_ok`` an unset scope is NULL,
  ``= ANY(NULL)`` is not true, and the row is filtered out: an absent scope
  hides everything instead of raising.

  RLS is no protection at all for a superuser or a role carrying BYPASSRLS,
  and the application role is one. The SQL executor therefore refuses to run
  on such a connection instead of trusting the DSN to be correct.

The reader role is granted conditionally. It is created operationally so its
password never lands in a migration, which means a development database
predating the SQL tool has no such role and the GRANT would abort the whole
upgrade. Skipping it is safe: with no role there is no reader connection, so
the policy is simply unused.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_csv_rows"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

READER_ROLE = "retriv_reader"
SCOPE_SETTING = "retriv.workspace_scope"


def upgrade() -> None:
    op.add_column(
        "processing_runs",
        sa.Column("row_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "processing_runs",
        sa.Column("csv_columns", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )

    op.create_table(
        "document_rows",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("processing_run_id", sa.Uuid(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column(
            "data", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["processing_run_id"],
            ["processing_runs.id"],
            name=op.f("fk_document_rows_processing_run_id_processing_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_rows")),
        sa.UniqueConstraint(
            "processing_run_id",
            "row_number",
            name="uq_document_rows_run_row",
        ),
    )
    op.create_index(
        "ix_document_rows_active_lookup",
        "document_rows",
        ["workspace_id", "processing_run_id"],
        unique=False,
    )
    # Filter predicates land in JSONB keys, so this index is what keeps
    # `data->>'status' = 'OPEN'` from scanning every workspace's rows.
    op.create_index(
        "ix_document_rows_data",
        "document_rows",
        ["data"],
        unique=False,
        postgresql_using="gin",
    )

    op.execute(
        "ALTER TABLE document_rows ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE document_rows FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        f"CREATE POLICY rows_in_scope ON document_rows "  # noqa: S608
        f"USING (workspace_id = ANY (current_setting('{SCOPE_SETTING}', true)::uuid[]))"
    )
    # `documents` is readable too, because a row citation has to name the file
    # it came from. It carries the same workspace column, so it takes the same
    # policy: granting the reader access to it without one would expose every
    # workspace's filenames and document ids.
    op.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE documents FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY documents_in_scope ON documents "  # noqa: S608
        f"USING (workspace_id = ANY (current_setting('{SCOPE_SETTING}', true)::uuid[]))"
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{READER_ROLE}') THEN
                GRANT USAGE ON SCHEMA public TO {READER_ROLE};
                GRANT SELECT ON document_rows, documents TO {READER_ROLE};
            END IF;
        END $$
        """  # noqa: S608 - interpolates READER_ROLE only, never input
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS documents_in_scope ON documents")
    op.execute("ALTER TABLE documents NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE documents DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS rows_in_scope ON document_rows")
    # Guarded for the same reason as the upgrade GRANT: the role is optional,
    # and revoking from a role that was never created aborts the downgrade.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{READER_ROLE}') THEN
                REVOKE SELECT ON document_rows, documents FROM {READER_ROLE};
            END IF;
        END $$
        """  # noqa: S608 - interpolates READER_ROLE only, never input
    )
    op.drop_index("ix_document_rows_data", table_name="document_rows")
    op.drop_index("ix_document_rows_active_lookup", table_name="document_rows")
    op.drop_table("document_rows")
    op.drop_column("processing_runs", "csv_columns")
    op.drop_column("processing_runs", "row_count")