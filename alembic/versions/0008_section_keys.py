"""Give every chunk a searchable section key, and record whether it is only a heading.

Retrieval completes a section once a search names it: the anchor's own chunks, then
its sub-sections. That needs two columns on ``document_chunks``.

``section_key`` is the JSON text of ``section_path``. The JSON column cannot answer
the question directly. PostgreSQL rejects ``jsonb[0:1]`` outright, and the jsonpath
spelling ``$[0] == "..."`` matches far more than the prefix it names -- on a
five-row fixture it returned every row, including ones whose path never mentioned
the value. Both are wrong for prefix semantics, which is what a subtree lookup needs.
Storing the encoded form alongside makes the subtree a plain indexed ``LIKE``.

``is_heading_only`` records the parser's own verdict. It replaces a length test that
compared a chunk's token count -- which includes the ancestor breadcrumb -- against a
ceiling, so a heading three levels deep was read as body text.

``section_key`` is NOT NULL with an empty default rather than backfilled. Existing
rows predate this revision and were chunked without it; their real keys are not
recoverable from the JSON column without the same prefix query this revision exists
to enable. Retrieval skips an empty key, so those chunks keep working as plain vector
matches and simply do not get section completion until their document is reprocessed.

Kept separate from ``0001`` deliberately: that revision is already applied to running
environments, and editing it would not replay there -- which is exactly how the first
attempt at this change reached a worker that inserted a column no migration had
created.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_section_keys"
down_revision: str | None = "0007_reader_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document_chunks",
        sa.Column(
            "section_key",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "document_chunks",
        sa.Column(
            "is_heading_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # text_pattern_ops is what makes the prefix form of LIKE indexable. Under the
    # default ops the planner cannot use the index for a pattern that does not
    # begin with the column value.
    op.create_index(
        "ix_document_chunks_section_key",
        "document_chunks",
        ["processing_run_id", "section_key"],
        postgresql_ops={"section_key": "text_pattern_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_document_chunks_section_key", table_name="document_chunks")
    op.drop_column("document_chunks", "is_heading_only")
    op.drop_column("document_chunks", "section_key")