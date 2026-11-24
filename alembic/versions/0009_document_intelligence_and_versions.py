"""Add document intelligence profiles and workspace-scoped document versioning tables.

``document_profiles`` stores canonical document intelligence (real title, category,
subtype, primary entities, counterparties, period, effective date, summary, and a
flattened ``search_text`` field) extracted once per active processing run.

``document_version_groups``, ``document_version_members``, and
``document_version_suggestions`` are workspace-scoped so revision relationships and
pending suggestions never leak across workspace boundaries.

Revision ID: 0009_document_intelligence
Revises: 0008_trgm_text_index
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009_document_intelligence"
down_revision: str | None = "0008_trgm_text_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

TRGM_PROFILE_INDEX = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_trgm') THEN
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
        CREATE INDEX IF NOT EXISTS ix_document_profiles_search_text_trgm
            ON public.document_profiles USING gin (search_text gin_trgm_ops);
    END IF;
END
$$;
"""


def upgrade() -> None:
    op.create_table(
        "document_profiles",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("processing_run_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="basic",
            nullable=False,
        ),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column(
            "doc_category",
            sa.String(length=64),
            server_default="other",
            nullable=False,
        ),
        sa.Column("doc_subtype", sa.String(length=120), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("primary_entities", JSON_TYPE, nullable=False),
        sa.Column("counterparties", JSON_TYPE, nullable=False),
        sa.Column("period_label", sa.String(length=64), nullable=True),
        sa.Column("effective_date", sa.String(length=32), nullable=True),
        sa.Column("document_number", sa.String(length=120), nullable=True),
        sa.Column("version_label", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("topics", JSON_TYPE, nullable=False),
        sa.Column("search_text", sa.Text(), server_default="", nullable=False),
        sa.Column("model", sa.String(length=200), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["processing_run_id"], ["processing_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("document_id"),
        sa.CheckConstraint("status IN ('basic', 'enriched')", name="document_profile_status"),
    )
    op.create_index("ix_document_profiles_category", "document_profiles", ["doc_category"])
    op.create_index(
        "ix_document_profiles_document_number", "document_profiles", ["document_number"]
    )

    op.create_table(
        "document_version_groups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "relation_kind",
            sa.String(length=32),
            server_default="supersedes",
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "relation_kind IN ('supersedes', 'periodic_series')",
            name="document_version_group_relation",
        ),
    )
    op.create_index(
        "ix_document_version_groups_workspace", "document_version_groups", ["workspace_id"]
    )

    op.create_table(
        "document_version_members",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("version_rank", sa.Integer(), server_default="1", nullable=False),
        sa.Column("is_latest", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("version_note", sa.String(length=255), nullable=True),
        sa.Column("linked_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["document_version_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["linked_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("workspace_id", "document_id"),
    )
    op.create_index(
        "ix_document_version_members_group",
        "document_version_members",
        ["group_id", "version_rank"],
    )
    op.create_index(
        "ix_document_version_members_workspace_latest",
        "document_version_members",
        ["workspace_id", "is_latest"],
    )

    op.create_table(
        "document_version_suggestions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("document_a_id", sa.Uuid(), nullable=False),
        sa.Column("document_b_id", sa.Uuid(), nullable=False),
        sa.Column("suggested_newer_document_id", sa.Uuid(), nullable=False),
        sa.Column(
            "suggested_relation",
            sa.String(length=32),
            server_default="supersedes",
            nullable=False,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reasons", JSON_TYPE, nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("resolved_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_a_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_b_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["suggested_newer_document_id"], ["documents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["resolved_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "document_a_id",
            "document_b_id",
            name="uq_document_version_suggestions_pair",
        ),
        sa.CheckConstraint(
            "suggested_relation IN ('supersedes', 'periodic_series')",
            name="document_version_suggestion_relation",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'dismissed')",
            name="document_version_suggestion_status",
        ),
    )
    op.create_index(
        "ix_document_version_suggestions_workspace_status",
        "document_version_suggestions",
        ["workspace_id", "status"],
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(TRGM_PROFILE_INDEX)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_document_profiles_search_text_trgm")
    op.drop_index(
        "ix_document_version_suggestions_workspace_status",
        table_name="document_version_suggestions",
    )
    op.drop_table("document_version_suggestions")
    op.drop_index(
        "ix_document_version_members_workspace_latest",
        table_name="document_version_members",
    )
    op.drop_index("ix_document_version_members_group", table_name="document_version_members")
    op.drop_table("document_version_members")
    op.drop_index("ix_document_version_groups_workspace", table_name="document_version_groups")
    op.drop_table("document_version_groups")
    op.drop_index("ix_document_profiles_document_number", table_name="document_profiles")
    op.drop_index("ix_document_profiles_category", table_name="document_profiles")
    op.drop_table("document_profiles")
