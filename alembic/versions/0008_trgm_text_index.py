"""Add a trigram index so search_text exact matches do not scan all chunk text.

`search_text` runs `text ILIKE '%query%'` on every call before the BM25
fallback, because an exact phrase always wins outright. Without an index that
is a sequential scan measured at ~21 ms per 4.1 MB of chunk text, growing
linearly with the corpus. A GIN trigram index turns it into an index lookup.

The extension ships in the standard postgres-contrib set, so the migration is
conditional only on its availability, matching the ParadeDB guard in 0006.

Revision ID: 0008_trgm_text_index
Revises: 0007_first_admin
Create Date: 2026-11-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_trgm_text_index"
down_revision: str | None = "0007_first_admin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRGM_INDEX = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_trgm') THEN
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
        CREATE INDEX IF NOT EXISTS ix_document_chunks_text_trgm
            ON public.document_chunks USING gin (text gin_trgm_ops);
    END IF;
END
$$;
"""


def upgrade() -> None:
    op.execute(TRGM_INDEX)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_text_trgm")
