from collections.abc import Sequence

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BASELINE_SCHEMA = """

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


CREATE TABLE public.agent_model_settings (
    id integer NOT NULL,
    base_url character varying(500) NOT NULL,
    api_key character varying(500) NOT NULL,
    model character varying(200) NOT NULL,
    last_test_ok boolean,
    last_test_model character varying(200),
    last_tested_at timestamp with time zone,
    updated_by_user_id uuid,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    provider character varying(32) DEFAULT 'custom'::character varying NOT NULL,
    CONSTRAINT ck_agent_model_settings_agent_model_settings_singleton CHECK ((id = 1))
);


CREATE SEQUENCE public.agent_model_settings_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.agent_model_settings_id_seq OWNED BY public.agent_model_settings.id;


CREATE TABLE public.api_keys (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    name character varying(100) NOT NULL,
    key_prefix character varying(16) NOT NULL,
    key_hash character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone
);


CREATE TABLE public.chunk_embeddings (
    chunk_id uuid NOT NULL,
    embedding_profile character varying(255) NOT NULL,
    origin_workspace_id uuid NOT NULL,
    document_id uuid NOT NULL,
    processing_run_id uuid NOT NULL,
    model character varying(255) NOT NULL,
    dimensions integer NOT NULL,
    content_hash character varying(64) NOT NULL,
    status character varying(32) DEFAULT 'pending'::character varying NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    last_error text,
    embedded_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    embedding public.vector(2000)
);


CREATE TABLE public.document_chunks (
    id uuid NOT NULL,
    origin_workspace_id uuid NOT NULL,
    document_id uuid NOT NULL,
    processing_run_id uuid NOT NULL,
    ordinal integer NOT NULL,
    text text NOT NULL,
    token_count integer NOT NULL,
    section_path jsonb,
    page_numbers jsonb,
    provenance jsonb,
    content_hash character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    section_key text DEFAULT ''::text NOT NULL,
    is_heading_only boolean DEFAULT false NOT NULL
);


CREATE TABLE public.document_images (
    id uuid NOT NULL,
    document_id uuid NOT NULL,
    ref character varying(100) NOT NULL,
    bucket character varying(255) NOT NULL,
    object_key character varying(1024) NOT NULL,
    media_type character varying(100) NOT NULL,
    size_bytes integer NOT NULL,
    page_number integer NOT NULL,
    describe_hint character varying(20) NOT NULL,
    caption character varying(500) NOT NULL,
    description text NOT NULL,
    described boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


CREATE TABLE public.document_rows (
    id uuid NOT NULL,
    origin_workspace_id uuid NOT NULL,
    document_id uuid NOT NULL,
    processing_run_id uuid NOT NULL,
    row_number integer NOT NULL,
    data jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    sheet character varying(100) DEFAULT ''::character varying NOT NULL
);

ALTER TABLE ONLY public.document_rows FORCE ROW LEVEL SECURITY;


CREATE TABLE public.document_workspaces (
    workspace_id uuid NOT NULL,
    document_id uuid NOT NULL,
    filename character varying(255) NOT NULL,
    created_by_user_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone
);

ALTER TABLE ONLY public.document_workspaces FORCE ROW LEVEL SECURITY;


CREATE TABLE public.documents (
    id uuid NOT NULL,
    origin_workspace_id uuid NOT NULL,
    original_filename character varying(255) NOT NULL,
    detected_media_type character varying(255) NOT NULL,
    size_bytes bigint NOT NULL,
    sha256 character varying(64) NOT NULL,
    bucket character varying(255) NOT NULL,
    object_key character varying(1024) NOT NULL,
    created_by_user_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    active_processing_run_id uuid
);

ALTER TABLE ONLY public.documents FORCE ROW LEVEL SECURITY;


CREATE TABLE public.processing_runs (
    id uuid NOT NULL,
    document_id uuid NOT NULL,
    origin_workspace_id uuid NOT NULL,
    job_kind character varying(32) NOT NULL,
    pipeline_version character varying(100) NOT NULL,
    status character varying(32) DEFAULT 'queued'::character varying NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    available_at timestamp with time zone DEFAULT now() NOT NULL,
    lease_until timestamp with time zone,
    lease_token uuid,
    artifact_bucket character varying(255),
    artifact_key character varying(1024),
    manifest_key character varying(1024),
    chunk_count integer DEFAULT 0 NOT NULL,
    embedded_chunk_count integer DEFAULT 0 NOT NULL,
    embedding_profile character varying(255),
    embedding_model character varying(255),
    embedding_dimensions integer,
    last_error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    row_count integer DEFAULT 0 NOT NULL,
    csv_columns jsonb,
    CONSTRAINT ck_processing_runs_processing_run_kind CHECK (((job_kind)::text = ANY ((ARRAY['extract'::character varying, 'delete'::character varying, 'access'::character varying])::text[])))
);

ALTER TABLE ONLY public.processing_runs FORCE ROW LEVEL SECURITY;


CREATE TABLE public.refresh_tokens (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    family_id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    auth_version integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone
);


CREATE TABLE public.users (
    id uuid NOT NULL,
    username character varying(255) NOT NULL,
    password_hash character varying(255) NOT NULL,
    role character varying(64) NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    auth_version integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


CREATE TABLE public.workspace_memberships (
    user_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    role character varying(32) NOT NULL,
    granted_by_user_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_workspace_memberships_workspace_membership_role CHECK (((role)::text = ANY ((ARRAY['owner'::character varying, 'editor'::character varying, 'viewer'::character varying])::text[])))
);


CREATE TABLE public.workspaces (
    id uuid NOT NULL,
    slug character varying(100) NOT NULL,
    name character varying(255) NOT NULL,
    description character varying(1000),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE ONLY public.agent_model_settings ALTER COLUMN id SET DEFAULT nextval('public.agent_model_settings_id_seq'::regclass);


ALTER TABLE ONLY public.agent_model_settings
    ADD CONSTRAINT pk_agent_model_settings PRIMARY KEY (id);


ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT pk_api_keys PRIMARY KEY (id);


ALTER TABLE ONLY public.chunk_embeddings
    ADD CONSTRAINT pk_chunk_embeddings PRIMARY KEY (chunk_id, embedding_profile);


ALTER TABLE ONLY public.document_chunks
    ADD CONSTRAINT pk_document_chunks PRIMARY KEY (id);


ALTER TABLE ONLY public.document_images
    ADD CONSTRAINT pk_document_images PRIMARY KEY (id);


ALTER TABLE ONLY public.document_rows
    ADD CONSTRAINT pk_document_rows PRIMARY KEY (id);


ALTER TABLE ONLY public.document_workspaces
    ADD CONSTRAINT pk_document_workspaces PRIMARY KEY (workspace_id, document_id);


ALTER TABLE ONLY public.documents
    ADD CONSTRAINT pk_documents PRIMARY KEY (id);


ALTER TABLE ONLY public.processing_runs
    ADD CONSTRAINT pk_processing_runs PRIMARY KEY (id);


ALTER TABLE ONLY public.refresh_tokens
    ADD CONSTRAINT pk_refresh_tokens PRIMARY KEY (id);


ALTER TABLE ONLY public.users
    ADD CONSTRAINT pk_users PRIMARY KEY (id);


ALTER TABLE ONLY public.workspace_memberships
    ADD CONSTRAINT pk_workspace_memberships PRIMARY KEY (user_id, workspace_id);


ALTER TABLE ONLY public.workspaces
    ADD CONSTRAINT pk_workspaces PRIMARY KEY (id);


ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT uq_api_keys_key_hash UNIQUE (key_hash);


ALTER TABLE ONLY public.document_chunks
    ADD CONSTRAINT uq_document_chunks_run_ordinal UNIQUE (processing_run_id, ordinal);


ALTER TABLE ONLY public.document_images
    ADD CONSTRAINT uq_document_images_document_ref UNIQUE (document_id, ref);


ALTER TABLE ONLY public.document_rows
    ADD CONSTRAINT uq_document_rows_run_sheet_row UNIQUE (processing_run_id, sheet, row_number);


ALTER TABLE ONLY public.documents
    ADD CONSTRAINT uq_documents_object_key UNIQUE (object_key);


ALTER TABLE ONLY public.documents
    ADD CONSTRAINT uq_documents_sha256 UNIQUE (sha256);


ALTER TABLE ONLY public.processing_runs
    ADD CONSTRAINT uq_processing_runs_identity UNIQUE (id, origin_workspace_id, document_id);


ALTER TABLE ONLY public.refresh_tokens
    ADD CONSTRAINT uq_refresh_tokens_token_hash UNIQUE (token_hash);


CREATE INDEX ix_api_keys_key_prefix ON public.api_keys USING btree (key_prefix);


CREATE INDEX ix_api_keys_user_id ON public.api_keys USING btree (user_id);


CREATE INDEX ix_chunk_embeddings_dispatch ON public.chunk_embeddings USING btree (processing_run_id, embedding_profile, status);


CREATE INDEX ix_chunk_embeddings_hnsw ON public.chunk_embeddings USING hnsw (embedding public.vector_cosine_ops);


CREATE INDEX ix_document_chunks_active_lookup ON public.document_chunks USING btree (origin_workspace_id, processing_run_id);


CREATE INDEX ix_document_chunks_document ON public.document_chunks USING btree (origin_workspace_id, document_id);


CREATE INDEX ix_document_chunks_section_key ON public.document_chunks USING btree (processing_run_id, section_key text_pattern_ops);


CREATE INDEX ix_document_images_document ON public.document_images USING btree (document_id);


CREATE INDEX ix_document_rows_active_lookup ON public.document_rows USING btree (origin_workspace_id, processing_run_id);


CREATE INDEX ix_document_rows_data ON public.document_rows USING gin (data);


CREATE INDEX ix_document_rows_document_sheet ON public.document_rows USING btree (document_id, sheet);


CREATE INDEX ix_document_workspaces_created_by_user_id ON public.document_workspaces USING btree (created_by_user_id);


CREATE INDEX ix_document_workspaces_document ON public.document_workspaces USING btree (document_id);


CREATE INDEX ix_document_workspaces_workspace_created ON public.document_workspaces USING btree (workspace_id, created_at);


CREATE INDEX ix_documents_active_run ON public.documents USING btree (active_processing_run_id);


CREATE INDEX ix_documents_created_by_user_id ON public.documents USING btree (created_by_user_id);


CREATE INDEX ix_documents_origin_workspace_created ON public.documents USING btree (origin_workspace_id, created_at);


CREATE INDEX ix_documents_origin_workspace_id ON public.documents USING btree (origin_workspace_id);


CREATE INDEX ix_processing_runs_dispatch ON public.processing_runs USING btree (status, available_at);


CREATE INDEX ix_processing_runs_document ON public.processing_runs USING btree (document_id);


CREATE INDEX ix_processing_runs_lease ON public.processing_runs USING btree (status, lease_until);


CREATE INDEX ix_processing_runs_origin_workspace_created ON public.processing_runs USING btree (origin_workspace_id, created_at);


CREATE INDEX ix_refresh_tokens_family_id ON public.refresh_tokens USING btree (family_id);


CREATE INDEX ix_refresh_tokens_user_id ON public.refresh_tokens USING btree (user_id);


CREATE UNIQUE INDEX ix_users_username ON public.users USING btree (username);


CREATE INDEX ix_workspace_memberships_workspace_user ON public.workspace_memberships USING btree (workspace_id, user_id);


CREATE UNIQUE INDEX ix_workspaces_slug ON public.workspaces USING btree (slug);


CREATE UNIQUE INDEX uq_processing_runs_one_active_extract ON public.processing_runs USING btree (document_id) WHERE (((job_kind)::text = 'extract'::text) AND ((status)::text = ANY ((ARRAY['queued'::character varying, 'fetching'::character varying, 'extracting'::character varying, 'describing'::character varying, 'storing_artifact'::character varying, 'chunking'::character varying, 'embedding'::character varying, 'retry_wait'::character varying])::text[])));


ALTER TABLE ONLY public.agent_model_settings
    ADD CONSTRAINT fk_agent_model_settings_updated_by_user_id_users FOREIGN KEY (updated_by_user_id) REFERENCES public.users(id) ON DELETE SET NULL;


ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT fk_api_keys_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.chunk_embeddings
    ADD CONSTRAINT fk_chunk_embeddings_chunk_id_document_chunks FOREIGN KEY (chunk_id) REFERENCES public.document_chunks(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.document_chunks
    ADD CONSTRAINT fk_document_chunks_processing_run_id_processing_runs FOREIGN KEY (processing_run_id) REFERENCES public.processing_runs(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.document_images
    ADD CONSTRAINT fk_document_images_document_id_documents FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.document_rows
    ADD CONSTRAINT fk_document_rows_processing_run_id_processing_runs FOREIGN KEY (processing_run_id) REFERENCES public.processing_runs(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.document_workspaces
    ADD CONSTRAINT fk_document_workspaces_created_by_user_id_users FOREIGN KEY (created_by_user_id) REFERENCES public.users(id) ON DELETE RESTRICT;


ALTER TABLE ONLY public.document_workspaces
    ADD CONSTRAINT fk_document_workspaces_document_id_documents FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.document_workspaces
    ADD CONSTRAINT fk_document_workspaces_workspace_id_workspaces FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.documents
    ADD CONSTRAINT fk_documents_created_by_user_id_users FOREIGN KEY (created_by_user_id) REFERENCES public.users(id) ON DELETE RESTRICT;


ALTER TABLE ONLY public.documents
    ADD CONSTRAINT fk_documents_workspace_id_workspaces FOREIGN KEY (origin_workspace_id) REFERENCES public.workspaces(id) ON DELETE RESTRICT;


ALTER TABLE ONLY public.processing_runs
    ADD CONSTRAINT fk_processing_runs_document_id_documents FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.refresh_tokens
    ADD CONSTRAINT fk_refresh_tokens_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.workspace_memberships
    ADD CONSTRAINT fk_workspace_memberships_granted_by_user_id_users FOREIGN KEY (granted_by_user_id) REFERENCES public.users(id) ON DELETE RESTRICT;


ALTER TABLE ONLY public.workspace_memberships
    ADD CONSTRAINT fk_workspace_memberships_user_id_users FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


ALTER TABLE ONLY public.workspace_memberships
    ADD CONSTRAINT fk_workspace_memberships_workspace_id_workspaces FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;


ALTER TABLE public.document_rows ENABLE ROW LEVEL SECURITY;


ALTER TABLE public.document_workspaces ENABLE ROW LEVEL SECURITY;


CREATE POLICY document_workspaces_in_scope ON public.document_workspaces USING ((workspace_id = ANY ((current_setting('retriv.workspace_scope'::text, true))::uuid[])));


ALTER TABLE public.documents ENABLE ROW LEVEL SECURITY;


CREATE POLICY documents_in_scope ON public.documents USING ((EXISTS ( SELECT 1
   FROM public.document_workspaces link
  WHERE ((link.document_id = documents.id) AND (link.workspace_id = ANY ((current_setting('retriv.workspace_scope'::text, true))::uuid[])) AND (link.deleted_at IS NULL)))));


ALTER TABLE public.processing_runs ENABLE ROW LEVEL SECURITY;


CREATE POLICY rows_in_scope ON public.document_rows USING ((EXISTS ( SELECT 1
   FROM public.document_workspaces link
  WHERE ((link.document_id = document_rows.document_id) AND (link.workspace_id = ANY ((current_setting('retriv.workspace_scope'::text, true))::uuid[])) AND (link.deleted_at IS NULL)))));


CREATE POLICY runs_in_scope ON public.processing_runs USING ((EXISTS ( SELECT 1
   FROM public.document_workspaces link
  WHERE ((link.document_id = processing_runs.document_id) AND (link.workspace_id = ANY ((current_setting('retriv.workspace_scope'::text, true))::uuid[])) AND (link.deleted_at IS NULL)))));


DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'retriv_reader') THEN
        RETURN;
    END IF;
    GRANT USAGE ON SCHEMA public TO retriv_reader;
    GRANT SELECT ON public.document_rows,
                    public.documents,
                    public.document_workspaces,
                    public.processing_runs TO retriv_reader;
END $$;"""

def split_statements(sql: str) -> list[str]:
    """Split the baseline into single statements for asyncpg.

    asyncpg refuses multi-statement executes, and a naive ``;`` split would
    cut inside the dollar-quoted grant block and inside string literals.
    This walks the text tracking line comments, single-quoted strings (with
    doubled-quote escapes) and ``$$`` dollar bodies, and breaks only on a
    semicolon at nesting level zero.
    """
    statements: list[str] = []
    buffer: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        character = sql[index]
        if sql.startswith("--", index):
            end = sql.find("\n", index)
            end = length if end == -1 else end
            buffer.append(sql[index:end])
            index = end
            continue
        if character == "'":
            end = index + 1
            while end < length:
                if sql[end] == "'":
                    if end + 1 < length and sql[end + 1] == "'":
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            buffer.append(sql[index:end])
            index = end
            continue
        if sql.startswith("$$", index):
            end = sql.find("$$", index + 2)
            end = length if end == -1 else end + 2
            buffer.append(sql[index:end])
            index = end
            continue
        if character == ";":
            statement = "".join(buffer).strip("\n")
            if statement.strip():
                statements.append(statement)
            buffer = []
            index += 1
            continue
        buffer.append(character)
        index += 1
    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


def upgrade() -> None:
    for statement in split_statements(BASELINE_SCHEMA):
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP SCHEMA public CASCADE")
    op.execute("CREATE SCHEMA public")
    op.execute("DROP EXTENSION IF EXISTS vector")
