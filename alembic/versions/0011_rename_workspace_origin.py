"""Rename the workspace columns that hold a document's origin.

After documents became shareable, ``workspace_id`` on the document and its
derived rows no longer means "the workspace that may read this". It means the
workspace where the raw object and artifacts were first stored. Access lives
in ``document_workspaces``. The old name kept reading like an authorization
scope, so it is renamed to say what it is.

``document_workspaces.workspace_id`` is deliberately not touched: that one is
the actual access link.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_rename_workspace_origin"
down_revision: str | None = "0010_document_workspaces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RENAMES = (
    ("documents", "workspace_id", "origin_workspace_id"),
    ("processing_runs", "workspace_id", "origin_workspace_id"),
    ("document_chunks", "workspace_id", "origin_workspace_id"),
    ("chunk_embeddings", "workspace_id", "origin_workspace_id"),
    ("document_rows", "workspace_id", "origin_workspace_id"),
)

INDEX_RENAMES = (
    ("ix_documents_workspace_id", "ix_documents_origin_workspace_id"),
    ("ix_documents_workspace_created", "ix_documents_origin_workspace_created"),
    (
        "ix_processing_runs_workspace_created",
        "ix_processing_runs_origin_workspace_created",
    ),
)


def upgrade() -> None:
    for table, old_name, new_name in RENAMES:
        op.alter_column(table, old_name, new_column_name=new_name)
    for old_name, new_name in INDEX_RENAMES:
        op.execute(f"ALTER INDEX {old_name} RENAME TO {new_name}")


def downgrade() -> None:
    for old_name, new_name in reversed(INDEX_RENAMES):
        op.execute(f"ALTER INDEX {new_name} RENAME TO {old_name}")
    for table, old_name, new_name in reversed(RENAMES):
        op.alter_column(table, new_name, new_column_name=old_name)
