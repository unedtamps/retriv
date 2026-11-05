"""Vectors move from Qdrant into Postgres as pgvector.

The vector lives on ``chunk_embeddings`` itself, next to the ledger row it
belonged to: one store, no point-id mapping, no payload to keep in sync.
The column is typed ``vector(2000)`` because pgvector's HNSW index tops out
at 2,000 dimensions for the ``vector`` type and the qwen3 embedding is
Matryoshka-trained, so retriv truncates its 4096-dim output to 2000 and
renormalizes before storing.

Re-embedding is not done here. The worker's embedding reconciliation already
detects a profile, model or dimensions drift against the settings and resets
the affected rows to ``pending`` under the new profile, so restarting the
stack after this migration regenerates every vector from stored chunk text.

Downgrade drops the column: vectors regenerable from chunk text, and the old
Qdrant contents they were copied from are gone with the service.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018_pgvector"
down_revision: str | None = "0017_agent_model_provider"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("ALTER TABLE chunk_embeddings ADD COLUMN embedding vector(2000)")
    op.execute(
        "CREATE INDEX ix_chunk_embeddings_hnsw "
        "ON chunk_embeddings USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute("ALTER TABLE chunk_embeddings DROP COLUMN qdrant_point_id")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunk_embeddings_hnsw")
    op.execute("ALTER TABLE chunk_embeddings DROP COLUMN IF EXISTS embedding")
