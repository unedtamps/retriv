"""Index document_rows by document, so a row query reads one file and not the table.

``RowQuery`` refuses any statement that does not name a ``document_id``, and
re-validates the result so it cannot span two files. Every query it will ever
accept is therefore already scoped to a single document -- but nothing made
that scope cheap to satisfy, because ``document_id`` carried no index. The
only indexes on the table were the primary key, the unique
``(processing_run_id, row_number)``, and ``(workspace_id,
processing_run_id)``, none of which a ``document_id = ...`` predicate can use.
Each accepted statement therefore began with a sequential scan of every row
ever written, across every file in every workspace the caller could see.

Measured on a table of 1,000,340 rows across ten files, for the shape of
statement the tool accepts (``where document_id = ...`` plus a JSONB
predicate): 89 ms sequential, 30 ms with this index, and the remaining cost is
the one file's rows rather than the table. That is the difference between
query cost growing with the workspace and query cost growing with the file,
which is the bound that matters: a single CSV is capped at
``max_csv_rows``, so a per-file query has a ceiling no matter how many files
accumulate.

The data is unchanged -- this is an index over rows that already exist, so
the build is proportional to current size and rewrites nothing.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_row_document_index"
down_revision: str | None = "0005_drop_agent_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_document_rows_document",
        "document_rows",
        ["document_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_document_rows_document", table_name="document_rows")