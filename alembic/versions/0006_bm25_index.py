"""Add the ParadeDB BM25 index when the extension is available.

The search path can stay on cosine/ILIKE without it, so the migration is
conditional: a plain Postgres image (including the test image without
``pg_search``) skips it, while the ParadeDB image creates the extension and
the index. The text-search tool uses this index once it is switched from the
substring query to the exact-first BM25 hybrid.

Revision ID: 0006_bm25_index
Revises: 0005_rerank_provider
Create Date: 2026-11-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_bm25_index"
down_revision: str | None = "0005_rerank_provider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BM25_INDEX = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_available_extensions WHERE name = 'pg_search'
    ) AND current_setting('shared_preload_libraries') LIKE '%pg_search%' THEN
        CREATE EXTENSION IF NOT EXISTS pg_search CASCADE;
        CREATE INDEX IF NOT EXISTS document_chunks_bm25
            ON document_chunks USING bm25 (id, text);
    END IF;
END
$$;
"""


def upgrade() -> None:
    op.execute(BM25_INDEX)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS document_chunks_bm25")
