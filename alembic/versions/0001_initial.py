"""Single initial schema.

The three services each carried an Alembic history against their own
database. There is one database now and no deployed data to preserve, so
the histories squash into this one revision rather than being replayed
eleven times.

What the merge changed, and why it is visible here:

- ``documents`` absorbs the ingestion service's ``source_documents``.
  Same row, two copies, kept in step by a signed webhook; the webhook is
  gone, so there is one copy.
- ``documents.status`` is gone. A document's state is derived from its
  active processing run, so the two cannot drift apart.
- ``processing_runs``, ``document_chunks`` and ``chunk_embeddings`` move
  in unchanged in substance.
- The foreign keys between the pipeline tables become single-column.
  They were composite because the referenced rows lived in another
  database; within one database the key alone is enough.
- ``outbox_events`` and ``event_inbox`` are gone. A document and its
  processing run commit in one transaction, so there is no boundary left
  to bridge and nothing to deduplicate on replay.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("auth_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(op.f("ix_users_username"), "users", ["username"], unique=True)

    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
    )
    op.create_index(op.f("ix_workspaces_slug"), "workspaces", ["slug"], unique=True)

    op.create_table(
        "workspace_memberships",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("granted_by_user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'editor', 'viewer')",
            name=op.f("ck_workspace_memberships_workspace_membership_role"),
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_user_id"],
            ["users.id"],
            name=op.f("fk_workspace_memberships_granted_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_workspace_memberships_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_workspace_memberships_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "workspace_id", name=op.f("pk_workspace_memberships")),
    )
    op.create_index(
        op.f("ix_workspace_memberships_workspace_user"),
        "workspace_memberships",
        ["workspace_id", "user_id"],
    )

    op.create_table(
        "processing_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("job_kind", sa.String(length=32), nullable=False),
        sa.Column("pipeline_version", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_token", sa.Uuid(), nullable=True),
        sa.Column("artifact_bucket", sa.String(length=255), nullable=True),
        sa.Column("artifact_key", sa.String(length=1024), nullable=True),
        sa.Column("manifest_key", sa.String(length=1024), nullable=True),
        sa.Column("chunk_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("embedded_chunk_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("embedding_profile", sa.String(length=255), nullable=True),
        sa.Column("embedding_model", sa.String(length=255), nullable=True),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "job_kind IN ('extract', 'delete')", name=op.f("ck_processing_runs_processing_run_kind")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processing_runs")),
        sa.UniqueConstraint(
            "id", "workspace_id", "document_id", name="uq_processing_runs_identity"
        ),
    )
    op.create_index(
        op.f("ix_processing_runs_dispatch"), "processing_runs", ["status", "available_at"]
    )
    op.create_index(
        op.f("ix_processing_runs_lease"), "processing_runs", ["status", "lease_until"]
    )
    op.create_index(op.f("ix_processing_runs_document"), "processing_runs", ["document_id"])
    op.create_index(
        op.f("ix_processing_runs_workspace_created"),
        "processing_runs",
        ["workspace_id", "created_at"],
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("detected_media_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("bucket", sa.String(length=255), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_processing_run_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_documents_created_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_documents_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("object_key", name=op.f("uq_documents_object_key")),
        sa.UniqueConstraint(
            "workspace_id", "sha256", name="uq_documents_workspace_sha256"
        ),
    )
    op.create_index(op.f("ix_documents_workspace_id"), "documents", ["workspace_id"])
    op.create_index(op.f("ix_documents_created_by_user_id"), "documents", ["created_by_user_id"])
    op.create_index(
        op.f("ix_documents_active_run"), "documents", ["active_processing_run_id"]
    )
    op.create_index(
        op.f("ix_documents_workspace_created"), "documents", ["workspace_id", "created_at"]
    )
    # Created after documents so the foreign key resolves. Intentionally
    # has no constraint: processing_runs.document_id already points at
    # documents, so a second one would make the pair circular.
    op.create_foreign_key(
        op.f("fk_processing_runs_document_id_documents"),
        "processing_runs",
        "documents",
        ["document_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("processing_run_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column(
            "section_path",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "page_numbers",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "provenance",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["processing_run_id"],
            ["processing_runs.id"],
            name=op.f("fk_document_chunks_processing_run_id_processing_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_chunks")),
        sa.UniqueConstraint(
            "processing_run_id", "ordinal", name="uq_document_chunks_run_ordinal"
        ),
    )
    op.create_index(
        op.f("ix_document_chunks_document"),
        "document_chunks",
        ["workspace_id", "document_id"],
    )
    op.create_index(
        op.f("ix_document_chunks_active_lookup"),
        "document_chunks",
        ["workspace_id", "processing_run_id"],
    )

    op.create_table(
        "chunk_embeddings",
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("embedding_profile", sa.String(length=255), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("processing_run_id", sa.Uuid(), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("qdrant_point_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["document_chunks.id"],
            name=op.f("fk_chunk_embeddings_chunk_id_document_chunks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("chunk_id", "embedding_profile", name=op.f("pk_chunk_embeddings")),
        sa.UniqueConstraint(
            "qdrant_point_id", name="uq_chunk_embeddings_qdrant_point_id"
        ),
    )
    op.create_index(
        op.f("ix_chunk_embeddings_dispatch"),
        "chunk_embeddings",
        ["processing_run_id", "embedding_profile", "status"],
    )
    op.create_index(
        op.f("ix_chunk_embeddings_document"),
        "chunk_embeddings",
        ["workspace_id", "document_id", "embedding_profile"],
    )

    op.create_table(
        "agent_conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=True),
        sa.Column("next_sequence", sa.Integer(), server_default="1", nullable=False),
        sa.Column("active_turn_id", sa.Uuid(), nullable=True),
        sa.Column("active_turn_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_agent_conversations_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_conversations")),
    )
    op.create_index(
        op.f("ix_agent_conversations_owner_updated"),
        "agent_conversations",
        ["owner_user_id", "updated_at"],
    )

    op.create_table(
        "agent_conversation_turns",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reservation_token", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column(
            "source_refs",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "source_workspace_ids",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), server_default="processing", nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('processing', 'completed', 'failed')",
            name=op.f("ck_agent_conversation_turns_agent_conversation_turn_status"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["agent_conversations.id"],
            name=op.f("fk_agent_conversation_turns_conversation_id_agent_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_conversation_turns")),
        sa.UniqueConstraint(
            "conversation_id", "request_id", name="uq_agent_turns_conversation_request"
        ),
        sa.UniqueConstraint(
            "conversation_id", "sequence", name="uq_agent_turns_conversation_sequence"
        ),
    )
    op.create_index(
        op.f("ix_agent_turns_conversation_sequence"),
        "agent_conversation_turns",
        ["conversation_id", "sequence"],
    )

    op.create_table(
        "provisioning_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("migration_id", sa.String(length=255), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_uri", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "result",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provisioning_runs")),
    )
    op.create_index(
        op.f("ix_provisioning_runs_migration_id"),
        "provisioning_runs",
        ["migration_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_provisioning_runs_created_at"), "provisioning_runs", ["created_at"]
    )


def downgrade() -> None:
    op.drop_table("provisioning_runs")
    op.drop_table("agent_conversation_turns")
    op.drop_table("agent_conversations")
    op.drop_table("chunk_embeddings")
    op.drop_table("document_chunks")
    op.drop_constraint(
        op.f("fk_processing_runs_document_id_documents"),
        "processing_runs",
        type_="foreignkey",
    )
    op.drop_table("documents")
    op.drop_table("processing_runs")
    op.drop_table("workspace_memberships")
    op.drop_table("workspaces")
    op.drop_table("users")
