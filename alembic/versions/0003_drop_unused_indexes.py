"""Drop indexes that no query uses.

``ix_api_keys_key_prefix`` backed a display-only column; key authentication
looks up ``key_hash``. ``ix_documents_origin_workspace_id`` and
``ix_documents_origin_workspace_created`` covered a filter no query issues;
the only select of ``origin_workspace_id`` is by ``sha256``.
``ix_document_images_document`` duplicated the leading column of
``uq_document_images_document_ref``. ``ix_document_rows_active_lookup``
covered a filter the row-query path never issues; deletes by run use
``uq_document_rows_run_sheet_row`` and reads filter by ``document_id``.

Revision ID: 0003_drop_unused_indexes
Revises: 0002_drop_access_jobs
Create Date: 2026-11-09
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_drop_unused_indexes"
down_revision: str | None = "0002_drop_access_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_api_keys_key_prefix", table_name="api_keys")
    op.drop_index("ix_documents_origin_workspace_id", table_name="documents")
    op.drop_index("ix_documents_origin_workspace_created", table_name="documents")
    op.drop_index("ix_document_images_document", table_name="document_images")
    op.drop_index("ix_document_rows_active_lookup", table_name="document_rows")


def downgrade() -> None:
    op.create_index(
        "ix_document_rows_active_lookup",
        "document_rows",
        ["origin_workspace_id", "processing_run_id"],
    )
    op.create_index("ix_document_images_document", "document_images", ["document_id"])
    op.create_index(
        "ix_documents_origin_workspace_created",
        "documents",
        ["origin_workspace_id", "created_at"],
    )
    op.create_index("ix_documents_origin_workspace_id", "documents", ["origin_workspace_id"])
    op.create_index("ix_api_keys_key_prefix", "api_keys", ["key_prefix"])
