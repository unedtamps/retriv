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
