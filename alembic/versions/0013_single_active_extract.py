"""At most one in-flight extract run per document, enforced by the database.

The worker already guarantees this in practice: `FOR UPDATE SKIP LOCKED`
keeps two workers from claiming the same run, the enqueue paths no-op on an
existing extract, and the reindex route refuses while a run is in flight. But
every one of those guards lives in application code, so a new code path that
inserted a run directly would get two extractions of one document, racing to
write the same chunks and artifacts.

This index makes that state unrepresentable. It is partial on two axes so it
does not forbid combinations that are legitimate:

* ``job_kind = 'extract'`` -- an ``access`` run (Qdrant payload refresh) is
  enqueued whenever a workspace link is created or restored, which can happen
  while an extraction is already running. Those two must remain able to
  coexist.
* the non-terminal statuses only -- once a run is ready, failed, cancelled or
  unsupported, a later extraction for the same document is normal.

A violation here means a bug somewhere else, so the index is created without
pre-cleaning: if an existing database already holds the forbidden state, the
migration fails and names the document, rather than silently picking a winner.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013_single_active_extract"
down_revision: str | None = "0012_document_images"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "uq_processing_runs_one_active_extract"

IN_FLIGHT_EXTRACT_STATUSES = (
    "queued",
    "fetching",
    "extracting",
    "describing",
    "storing_artifact",
    "chunking",
    "embedding",
    "retry_wait",
)


def upgrade() -> None:
    statuses = ", ".join(f"'{status}'" for status in IN_FLIGHT_EXTRACT_STATUSES)
    op.execute(
        f"""
        CREATE UNIQUE INDEX {INDEX_NAME}
        ON processing_runs (document_id)
        WHERE job_kind = 'extract' AND status IN ({statuses})
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
