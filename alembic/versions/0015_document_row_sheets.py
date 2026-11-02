"""A tabular document's rows carry the sheet they came from.

A CSV is one unnamed sheet and a workbook is several named ones; both land
in ``document_rows``, and the ``sheet`` column is what keeps them apart.
Empty for a CSV, so no existing row changes identity.

Two constraints move with it. The old unique pair
``(processing_run_id, row_number)`` would reject the second sheet's row 1,
so it widens to ``(processing_run_id, sheet, row_number)``. The lookup that
serves ``document_id`` + ``sheet`` filters gets its own composite index, in
place of the single-column one it replaces.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0015_document_row_sheets"
down_revision: str | None = "0014_refresh_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document_rows",
        sa.Column("sheet", sa.String(length=100), nullable=False, server_default=""),
    )
    op.drop_constraint("uq_document_rows_run_row", "document_rows", type_="unique")
    op.create_unique_constraint(
        "uq_document_rows_run_sheet_row",
        "document_rows",
        ["processing_run_id", "sheet", "row_number"],
    )
    op.drop_index("ix_document_rows_document", table_name="document_rows")
    op.create_index(
        "ix_document_rows_document_sheet",
        "document_rows",
        ["document_id", "sheet"],
    )


def downgrade() -> None:
    op.drop_index("ix_document_rows_document_sheet", table_name="document_rows")
    op.create_index(
        "ix_document_rows_document", "document_rows", ["document_id"], unique=False
    )
    op.drop_constraint("uq_document_rows_run_sheet_row", "document_rows", type_="unique")
    op.create_unique_constraint(
        "uq_document_rows_run_row", "document_rows", ["processing_run_id", "row_number"]
    )
    op.drop_column("document_rows", "sheet")
