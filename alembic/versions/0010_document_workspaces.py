"""Let one canonical document belong to many workspaces.

A document is now identified by its content hash. ``document_workspaces`` is
the access link: the same object, chunks and vectors serve every linked
workspace, while the filename stays per link. The existing workspace column
on ``documents`` becomes the origin workspace, which is where the object and
artifacts live, not who may read them.

The old uniqueness rule was ``(workspace_id, sha256)``; it becomes a global
``sha256`` so uploading the same bytes to a second workspace attaches a link
instead of creating a second pipeline. Existing rows are backfilled with one
link each. A database that already contains cross-workspace duplicates must be
reset before this migration: the global unique constraint cannot be created
while two documents share a hash.

Row-level security for CSV access moves from the document's origin workspace
to the link table. ``retriv_reader`` keeps SELECT on the same tables plus the
link table, and the policies now ask whether an in-scope link exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_document_workspaces"
down_revision: str | None = "0009_drop_provisioning_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCOPE_SETTING = "retriv.workspace_scope"
READER_ROLE = "retriv_reader"


def upgrade() -> None:
    op.create_table(
        "document_workspaces",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_document_workspaces_created_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_workspaces_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_document_workspaces_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "document_id",
            name=op.f("pk_document_workspaces"),
        ),
    )
    op.create_index(
        "ix_document_workspaces_document",
        "document_workspaces",
        ["document_id"],
        unique=False,
    )
    op.create_index(
        "ix_document_workspaces_workspace_created",
        "document_workspaces",
        ["workspace_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_document_workspaces_created_by_user_id"),
        "document_workspaces",
        ["created_by_user_id"],
        unique=False,
    )

    # One link per existing document. A deployment that already has
    # cross-workspace duplicates must be reset before the global unique
    # constraint below can be applied.
    op.execute(
        """
        INSERT INTO document_workspaces
            (workspace_id, document_id, filename, created_by_user_id, created_at, deleted_at)
        SELECT workspace_id, id, original_filename, created_by_user_id, created_at, deleted_at
        FROM documents
        ON CONFLICT DO NOTHING
        """
    )

    op.drop_constraint(
        "uq_documents_workspace_sha256", "documents", type_="unique"
    )
    op.create_unique_constraint("uq_documents_sha256", "documents", ["sha256"])

    op.drop_constraint("processing_run_kind", "processing_runs", type_="check")
    op.create_check_constraint(
        "processing_run_kind",
        "processing_runs",
        "job_kind IN ('extract', 'delete', 'access')",
    )

    # Link table is readable by the restricted row-query role, but only for
    # links in the session's scope. The policies on the tables below ask this
    # table whether the document is in scope.
    op.execute("ALTER TABLE document_workspaces ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE document_workspaces FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY document_workspaces_in_scope ON document_workspaces "  # noqa: S608
        f"USING (workspace_id = ANY (current_setting('{SCOPE_SETTING}', true)::uuid[]))"
    )

    op.execute("DROP POLICY IF EXISTS rows_in_scope ON document_rows")
    op.execute(
        f"CREATE POLICY rows_in_scope ON document_rows "  # noqa: S608
        f"USING (EXISTS ("
        f"  SELECT 1 FROM document_workspaces link"
        f"  WHERE link.document_id = document_rows.document_id"
        f"    AND link.workspace_id = ANY (current_setting('{SCOPE_SETTING}', true)::uuid[])"
        f"    AND link.deleted_at IS NULL"
        f"))"
    )

    op.execute("DROP POLICY IF EXISTS documents_in_scope ON documents")
    op.execute(
        f"CREATE POLICY documents_in_scope ON documents "  # noqa: S608
        f"USING (EXISTS ("
        f"  SELECT 1 FROM document_workspaces link"
        f"  WHERE link.document_id = documents.id"
        f"    AND link.workspace_id = ANY (current_setting('{SCOPE_SETTING}', true)::uuid[])"
        f"    AND link.deleted_at IS NULL"
        f"))"
    )

    op.execute("DROP POLICY IF EXISTS runs_in_scope ON processing_runs")
    op.execute(
        f"CREATE POLICY runs_in_scope ON processing_runs "  # noqa: S608
        f"USING (EXISTS ("
        f"  SELECT 1 FROM document_workspaces link"
        f"  WHERE link.document_id = processing_runs.document_id"
        f"    AND link.workspace_id = ANY (current_setting('{SCOPE_SETTING}', true)::uuid[])"
        f"    AND link.deleted_at IS NULL"
        f"))"
    )

    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{READER_ROLE}') THEN
                GRANT USAGE ON SCHEMA public TO {READER_ROLE};
                GRANT SELECT ON document_workspaces TO {READER_ROLE};
            END IF;
        END $$
        """  # noqa: S608 - interpolates READER_ROLE only, never input
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS runs_in_scope ON processing_runs")
    op.execute(
        f"CREATE POLICY runs_in_scope ON processing_runs "  # noqa: S608
        f"USING (workspace_id = ANY (current_setting('{SCOPE_SETTING}', true)::uuid[]))"
    )
    op.execute("DROP POLICY IF EXISTS documents_in_scope ON documents")
    op.execute(
        f"CREATE POLICY documents_in_scope ON documents "  # noqa: S608
        f"USING (workspace_id = ANY (current_setting('{SCOPE_SETTING}', true)::uuid[]))"
    )
    op.execute("DROP POLICY IF EXISTS rows_in_scope ON document_rows")
    op.execute(
        f"CREATE POLICY rows_in_scope ON document_rows "  # noqa: S608
        f"USING (workspace_id = ANY (current_setting('{SCOPE_SETTING}', true)::uuid[]))"
    )
    op.execute("DROP POLICY IF EXISTS document_workspaces_in_scope ON document_workspaces")
    op.execute("ALTER TABLE document_workspaces NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE document_workspaces DISABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{READER_ROLE}') THEN
                REVOKE SELECT ON document_workspaces FROM {READER_ROLE};
            END IF;
        END $$
        """  # noqa: S608 - interpolates READER_ROLE only, never input
    )

    op.drop_constraint("processing_run_kind", "processing_runs", type_="check")
    op.create_check_constraint(
        "processing_run_kind",
        "processing_runs",
        "job_kind IN ('extract', 'delete')",
    )
    op.drop_constraint("uq_documents_sha256", "documents", type_="unique")
    op.create_unique_constraint(
        "uq_documents_workspace_sha256", "documents", ["workspace_id", "sha256"]
    )
    op.drop_table("document_workspaces")
