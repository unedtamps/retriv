# Retriv

Backend untuk menerima dokumen perusahaan dengan scope per workspace, mengindeksnya, dan
menjawab pertanyaan di atasnya.

Satu aplikasi. Satu database. Dua entrypoint lain: worker pemrosesan dan provisioning.

```
app/          the HTTP application: auth, documents, processing status, answers
worker/       the extraction worker: Docling, chunking, embedding (separate process)
provisioning/ the one-shot CLI that creates the first users and workspaces
alembic/      one migration history, one database
web/          the dashboard
```

```mermaid
flowchart LR
    subgraph host["host"]
        W["web<br/>Vite :5173"]
        A["app<br/>uvicorn :8000"]
        P["provisioning<br/>one-shot CLI"]
    end
    subgraph docker["docker"]
        K["worker<br/>python -m worker"]
    end
    subgraph data["docker, loopback only"]
        DB[("PostgreSQL<br/>:5432")]
        Q[("Qdrant<br/>:6333")]
        M[("MinIO<br/>:9000")]
        C["ClamAV<br/>:3310"]
    end
    subgraph external["external"]
        L["LM Studio<br/>:1234"]
        O["chat model<br/>OpenAI-compatible"]
    end

    W -->|"/v1 with a bearer token"| A
    A --> DB
    A --> Q
    A --> M
    A --> C
    A -->|HTTPS| O
    K --> DB
    K --> Q
    K -->|artifact bucket| M
    K --> L
    A --> L
    P -->|manifest, advisory lock| DB
    P --> M

    style A fill:#2d5a3d,color:#fff
    style K fill:#5a452d,color:#fff
```

Solid arrows are the only thing crossing a process boundary now, and all of them point
at infrastructure. The three application processes reach each other through `app/`'s own
imports.

## Why one application

It used to be three services with their own databases, talking over HTTP. That bought a
lot of machinery for no benefit: a shared token, an HMAC-signed webhook, a transactional
outbox, an event inbox, an RS256 scope keypair, and a read-only database role — all to let
one process call another on the same host. Roughly sixteen environment variables whose
only job was to prove the caller was who it said it was.

The answer path is still scoped. A question is answered only from workspaces the caller
has `search_context` on, and the retriever re-checks that against every Qdrant point and
every PostgreSQL row. What changed is that the scope is now a function argument derived
from the caller's own memberships, rather than a token that had to be signed and
verified.

The worker stays a separate process for a concrete reason: Docling and PyTorch need
about 1.7 GB, an 8 GiB parser memory cap, and a sandboxed subprocess per document. Keeping
that in its own container is what keeps the API image around 250 MB and keeps a hostile
PDF away from the request path.

## Scope

- JWT access tokens valid for 60 minutes.
- The first users are created by a one-shot provisioning manifest; later users by a
  platform admin.
- `admin` is a platform role for the dashboard and grants no workspace data access.
- Workspace access lives in a membership table with role `owner`, `editor`, or `viewer`.
- Every authenticated user can see the workspace catalogue; document data requires a
  matching membership.
- Identical bytes in the same workspace are deduplicated by SHA-256.
- Uploads default to 50 MiB and 500 pages; encrypted and macro-enabled documents are
  rejected, and ClamAV scans before anything is stored.
- `PDF` and `DOCX` are chunked and embedded. `CSV` is stored as rows and queried with
  SQL; it is never embedded. `XLSX` is not accepted at all, because nothing could read
  it.
- A document's status is derived from its active processing run. There is no status
  column, so the document and the pipeline cannot disagree.

## Prerequisites

- Docker Engine and Docker Compose v2.
- `uv` for local test/lint, or Python 3.12+.
- An OpenAI-compatible embedding server on `127.0.0.1:1234` that is reachable from the
  worker container.
- A chat API key for answers, and the model id to use with it. The endpoint
  need not be OpenRouter: the harness picks `OpenRouterModel` or `OpenAIChatModel` from
  `CHAT_BASE_URL`, so any OpenAI-compatible service works.
- About 4 GiB of RAM for ClamAV on first startup.

## Running locally

```bash
cp .env.example .env
```

Fill in every `CHANGE ME`. The only one that cannot be generated for you is the chat
key; the rest are `openssl rand -hex 32`. Until the chat key and model are set, uploads
and indexing work and questions return a provider error.

```bash
make migrate   # start infrastructure and apply the schema
make api       # start the worker, then run the API on :8000
```

`make api` builds and starts the worker in Docker, so the first run takes a while: the
image is ~1.7 GB and the Docling layout and tableformer models are downloaded into it.

The dashboard is a separate npm project:

```bash
cd web && npm ci && npm run dev
```

It is at `http://127.0.0.1:5173`.

ClamAV's first start downloads its signature database and can take a few minutes.
PostgreSQL, Qdrant, MinIO, and ClamAV publish on loopback only.

## Data

Everything is in Docker named volumes under the `retriv-api` project:

- `retriv-api_postgres_data`
- `retriv-api_qdrant_data`
- `retriv-api_minio_data`
- `retriv-api_clamav_data`

Nothing is bind-mounted. `docker compose down` keeps the data; `docker compose down -v`
deletes all of it.

## Asking a question

A turn has two possible doors into the documents. `search_context` retrieves prose by
meaning; `query_rows` runs SQL over tabular rows. The model is offered both and picks,
which is why the catalogue is in every prompt it sees.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant A as app
    participant H as harness
    participant L as chat model
    participant E as LM Studio
    participant Q as Qdrant
    participant DB as PostgreSQL

    U->>A: POST /v1/agent/ask
    A->>DB: derive workspace_ids from memberships
    A->>H: answer(question, workspace_ids)
    H->>H: read the CSV catalogue (up to 100 files)
    H->>L: question + catalogue + search_context + query_rows
    L-->>H: search_context or query_rows
    H->>E: embed the query, if searching
    E-->>H: vector
    H->>Q: search, filter workspace_id IN scope
    H->>DB: re-validate every point against PostgreSQL
    H->>L: validated evidence
    H->>DB: or run SQL as the reader role, read-only, RLS applies the scope
    DB-->>H: rows, or values for an aggregate
    H->>L: evidence, one citable source per file
    L-->>H: an answer citing [1], [2]
    H-->>A: answer + sources
    A->>A: refuse if any source is outside the scope
    A-->>U: the answer with its sources
```

Four things keep this honest, and all four survive the merge:

1. **The scope is derived from the caller's own membership table**, not from a
   client-supplied list. There is no signed token to forge, because there is no second
   service to receive one.
2. **The `search_context` tool accepts a query and nothing else.** A model has no
   argument with which to argue for a wider scope.
3. **Every Qdrant point is re-validated against PostgreSQL**, including a recomputed
   `content_hash`. A vector whose chunk is gone, stale, or from a superseded run never
   reaches the model.
4. **The route re-checks the sources on the way out.** Reaching that check means the
   harness itself drifted, so the whole answer is refused rather than returned in part.

A turn that cited a workspace the caller has since lost stays visible in their private
transcript, but its sources are blanked and it is never replayed as context. Greeting-only
messages skip retrieval entirely, so a user with no workspace can still say hello.

### Asking a spreadsheet question

`query_rows` is a different door into the data, and it is worth being precise about what
protects it. The statement is written by the model, so it is not the argument validation
that keeps it in bounds — four layers below it do:

```mermaid
flowchart TD
    Q["model writes a statement"] --> A["table allowlist<br/>document_rows, documents"]
    A --> B["read-only transaction,<br/>statement_timeout"]
    B --> C["retriv_reader role:<br/>SELECT only, no BYPASSRLS"]
    C --> D["RLS policy derives the<br/>workspace from the role, not the SQL"]
    D --> R["rows returned"]

    style A stroke-dasharray:4
    style D stroke-width:2px
```

The dashed layer is the weakest one and is not what isolates anything. Deleting the
allowlist would not let the model read a table it lacks a grant on; the model cannot
issue `SET`, cannot open a transaction, and cannot write. What the allowlist does remove
is a table that is *granted* but should not be *mentioned* — `processing_runs` carries
`lease_token` and `last_error`, so the wrapper reads it to learn a file's columns while
the model cannot name it at all.

`verify_reader()` refuses to run if the configured role turns out to be `SUPERUSER` or to
hold `BYPASSRLS`, which would make every layer above irrelevant.

The role is set up by migration `0003_row_catalogue`, which grants `SELECT` on the three
tables a query legitimately needs and enables `FORCE ROW LEVEL SECURITY` on all of them.
Point `SQL_DATABASE_URL` at that role, not at the application DSN: the tool works against
a full-privilege connection, but then nothing below is doing any work. Leaving the setting
unset is the safe default — the tool is never registered and the model cannot reach a
table, while the catalogue still lists the files through the app's own session so it can
tell the user the data exists but is not queryable.

Two rules keep a query from silently answering a question about the wrong file. A query
must mention a `document_id` literal, and rows returning more than one `document_id` are
rejected. Aggregates return no rows, so they are exempt from the second check and the
literal check is the only guard — a query using `ANY(array[...])` over two ids would
slip past it. That gap is real and still open; a fix would parse the id out of the
statement rather than searching its text.

### How a CSV answer is cited

An aggregate has no row to point at, so it cannot be cited the way a quoted line is. It
is cited against **the file it was computed from**, once:

```mermaid
flowchart LR
    subgraph "one group-by over 7 columns"
        Q1["q1: sum by quarter"] --> S
        Q2["q2: min/max check"] --> S
        Q3["q3: row count"] --> S
    end
    S["ledger: one ContextSource<br/>chunk_id = uuid5(document_id, 'aggregate')"] --> U["1 citation"]
    style U stroke-width:2px
```

The constant namespace is the point. A group-by that returns seven rows is still one
piece of evidence, and emitting a source per computed row filled the citation list with
twenty-one entries for the same file — most of them from the model checking its own
arithmetic. Two different files still yield two citations, which is correct: they are
different evidence.

The locator follows from the same rule. A source is located by page if it has one, by
row if it has one, and by nothing at all if it has neither — which is the case for every
aggregate and for every DOCX chunk, since Docling emits no `page_no` for DOCX. The older
"Page unavailable" string described a document kind rather than a fault, so it named the
wrong thing for every source that lacked pages.

## How a document becomes searchable

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant W as Dashboard
    participant A as app (FastAPI)
    participant C as ClamAV
    participant M as MinIO
    participant DB as PostgreSQL
    participant K as worker
    participant L as LM Studio
    participant Q as Qdrant

    U->>W: choose a PDF, DOCX or CSV
    W->>A: POST /v1/workspaces/{id}/documents
    A->>A: stage to a temp file, hash it
    A->>C: scan the temp file
    alt malware found
        C-->>A: infected
        A-->>W: 422 malware_detected
    end
    A->>A: validate media type, pages, macros
    A->>DB: SELECT by (workspace_id, sha256)
    alt already stored and not being deleted
        DB-->>A: existing row
        A-->>W: 200 with duplicate=true
    end
    A->>M: PUT raw-documents/workspaces/{ws}/objects/{sha}/{id}
    A->>DB: BEGIN
    A->>DB: INSERT documents
    A->>DB: INSERT processing_runs (status=queued)
    A->>DB: COMMIT
    A-->>W: 202 Accepted
    W-->>U: "processing"

    loop every 2s
        K->>DB: SELECT ... WHERE status IN (queued, retry_wait) FOR UPDATE SKIP LOCKED
    end
    K->>K: claim the run, set lease_until and lease_token
    K->>M: GET the raw object
    alt PDF or DOCX
        K->>K: Docling extraction in a sandboxed subprocess
        K->>M: PUT the artifact and its manifest
        K->>DB: INSERT document_chunks
        loop in batches
            K->>L: POST /embeddings
            L-->>K: vectors
            K->>Q: upsert points, each payload carrying its workspace and run
        end
    else CSV
        K->>K: stdlib csv reader into memory, one dict per row
        loop in batches of 1000
            K->>DB: INSERT document_rows (data as JSONB)
        end
        Note over K,DB: no artifact, no chunks, no embeddings, no Qdrant
    end
    K->>DB: BEGIN, delete rows from superseded runs, then set<br/>status=ready and document.active_processing_run_id = run
    K->>DB: COMMIT
```

A CSV takes the rows path, not the chunk path. It is tabular data the model will
aggregate rather than prose it will quote, so it is stored verbatim as JSONB and
queried with SQL. Nothing is embedded, which is why the run reports `row_count = 320`
rather than a chunk count.

A document cannot exist without a way to be processed: the row and the run commit
together or not at all. There is no outbox and no retry-on-replay, because there is no
network hop where an event could be lost.

The run is not a signal — the worker **polls** every 2 seconds. `LISTEN`/`NOTIFY` was
rejected on purpose: the payload limit is 8 KB, the notification is delivered at commit
and is lost forever with no reader, so you would end up running both mechanisms anyway.

### The run state machine

```mermaid
stateDiagram-v2
    [*] --> queued: upload commits
    queued --> fetching: worker claims it
    queued --> unsupported_format: media type has no text to read
    fetching --> extracting
    extracting --> row_storing: media type is CSV
    extracting --> storing_artifact: PDF or DOCX
    row_storing --> ready: rows committed
    storing_artifact --> chunking
    chunking --> embedding
    embedding --> ready: all vectors upserted
    queued --> cancelled: document deleted first
    fetching --> retry_wait: transient failure
    extracting --> retry_wait: transient failure
    row_storing --> retry_wait: transient failure
    storing_artifact --> retry_wait: transient failure
    chunking --> retry_wait: transient failure
    embedding --> retry_wait: embedding or Qdrant unavailable
    retry_wait --> fetching: available_at reached
    retry_wait --> failed: attempts exhausted
    fetching --> failed: permanent failure
    extracting --> failed: permanent failure
    queued --> ready: DELETE run
    ready --> [*]
    failed --> [*]
    cancelled --> [*]
    unsupported_format --> [*]
```

A lease is held for 30 minutes and heartbeated every third of that. A run whose lease
expired is reclaimed by the next worker, which is what makes a worker crash recoverable
without a coordinator. Transient failures back off exponentially, capped at 300s.

## Deleting a document

The ordering is the whole point.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant A as app
    participant M as MinIO
    participant DB as PostgreSQL
    participant K as worker
    participant Q as Qdrant

    U->>A: DELETE /v1/workspaces/{id}/documents/{id}
    A->>DB: SELECT ... FOR UPDATE
    A->>DB: UPDATE documents SET deleted_at
    A->>DB: CANCEL any in-flight extract run
    A->>DB: INSERT processing_runs (job_kind=delete, status=queued)
    A->>DB: COMMIT
    A->>M: DELETE the raw object
    A-->>U: 204 No Content

    Note over U,A: the row still exists and the UI shows deleting

    U->>A: re-upload the same bytes
    A-->>U: 409 document_deletion_in_progress

    K->>Q: delete every point for the document
    K->>M: delete the artifact prefix
    K->>DB: DELETE the document row
    Note right of DB: cascades to chunks and the embedding ledger

    U->>A: re-upload the same bytes
    A-->>U: 202 Accepted
```

The Qdrant point IDs live in `chunk_embeddings`, which cascades away with the document.
Deleting the row first would orphan every vector the document ever produced, with no
record of where to find them. So the row outlives the request and dies last.

While the cleanup is in flight the `(workspace_id, sha256)` unique constraint still
holds the row, which is why an identical re-upload is refused rather than silently
creating a duplicate.

## Asking a question about a document

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant A as app
    participant DB as PostgreSQL
    participant H as harness
    participant L as chat model
    participant E as LM Studio
    participant Q as Qdrant

    U->>A: POST /v1/agent/ask
    A->>DB: SELECT workspace_ids from memberships<br/>where role has search_context
    alt the user is in no searchable workspace
        A-->>U: 403 context_access_required
    end
    A->>H: answer(question, workspace_ids)

    loop at most 10 tool calls, 3 model requests each
        H->>L: messages + the search_context tool
        L-->>H: calls search_context with a query
        H->>E: embed the query
        E-->>H: vector
        H->>Q: search, filter workspace_id IN scope AND profile matches
        Q-->>H: points and payloads
        H->>DB: re-validate every point: the document is not deleted,<br/>the run is the active one, the chunk is indexed,<br/>profile and dimensions match, content_hash matches
        H->>H: reject the result if any point is out of scope<br/>or fails validation
        H->>L: the validated evidence
        L-->>H: an answer citing [1], [2]
    end

    H-->>A: answer + sources
    A->>A: refuse if any source is outside the scope
    A-->>U: the answer with its sources
```

Four things keep this honest, and all four survive the merge:

1. **The scope is derived from the caller's own membership table**, not from a
   client-supplied list. There is no signed token to forge, because there is no second
   service to receive one.
2. **The `search_context` tool accepts a query and nothing else.** A model has no
   argument with which to argue for a wider scope.
3. **Every Qdrant point is re-validated against PostgreSQL**, including a recomputed
   `content_hash`. A vector whose chunk is gone, stale, or from a superseded run never
   reaches the model.
4. **The route re-checks the sources on the way out.** Reaching that check means the
   harness itself drifted, so the whole answer is refused rather than returned in part.

A turn that cited a workspace the caller has since lost stays visible in their private
transcript, but its sources are blanked and it is never replayed as context. Greeting-only
messages skip retrieval entirely, so a user with no workspace can still say hello.

The three tool limits are one budget, and the evidence a turn can actually hold is
`min(MAX_CURRENT_SOURCES, MAX_TOOL_CALLS * (max_context_chunks + heading_expand_chunks))`.
The second term binds first, so raising the source ceiling alone does nothing. A test
asserts the inequality so the two cannot drift apart silently.

## Authentication and revocation

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant W as Dashboard
    participant A as app
    participant DB as PostgreSQL

    U->>W: username and password
    W->>A: POST /v1/auth/token
    A->>DB: SELECT the user row
    A->>A: verify the Argon2 hash
    A-->>W: HS256 JWT carrying sub, role, auth_version, exp

    W->>A: any request with the bearer token
    A->>A: verify the signature, issuer, audience, expiry
    A->>DB: SELECT the user row again
    A->>A: is_active, and auth_version still matches the claim
    alt the row is gone, disabled, or the version moved
        A-->>W: 401
    end
    A-->>W: the request proceeds
```

`auth_version` is the reason a password reset or a provisioning change takes effect
immediately rather than at token expiry: the claim is compared against the row on every
request, so bumping the counter invalidates every outstanding token at once. The
`admin` platform role is orthogonal to workspace roles and grants no data access.

## Provisioning

```mermaid
sequenceDiagram
    autonumber
    actor P as Operator
    participant M as MinIO
    participant DB as PostgreSQL
    participant X as the provisioning CLI

    P->>M: upload users-v1.json to the manifest bucket
    P->>X: run it with the manifest URI
    X->>M: GET the manifest
    X->>X: parse and validate, extra="forbid"
    X->>DB: BEGIN
    X->>DB: pg_advisory_xact_lock(hash of migration_id)
    alt migration_id already applied
        X->>DB: compare the payload hash
        X-->>P: no-op, or 409 if the content differs
    end
    X->>DB: upsert users, upsert workspaces
    X->>DB: auth_version = auth_version + 1 for every changed user
    X->>DB: COMMIT
    X-->>P: a JSON summary
```

It reads the manifest over S3, using the read-only `migration-reader` identity that
`minio-init` creates. That identity cannot reach `raw-documents` or
`context-documents`, so a manifest you have not reviewed cannot read your documents.

```bash
uv run --env-file .env -- python -m provisioning
```

The `--env-file` is required and not optional politeness: the job's settings class sets
`env_file=None`, so it reads the environment and nothing else. A one-shot with write
access to every user row should not silently pick up credentials because it ran in this
directory. Its variables carry a `PROVISIONER_` prefix and are in `.env.example`.

Two things to know before you use it:

- **It creates workspaces with no members.** Assigning the first owner is an admin API
  call: `PUT /v1/admin/workspaces/{id}/members/{user_id}`. A provisioned workspace is
  otherwise unreachable.
- **It overwrites credentials.** A username that already exists has its password hash,
  role, and active flag reset from the manifest, and its `auth_version` bumped, so every
  live session for that user is signed out.

## The shape of the data

```mermaid
erDiagram
    users ||--o{ workspace_memberships : "joins"
    workspaces ||--o{ workspace_memberships : "has"
    workspaces ||--o{ documents : "contains"
    users ||--o{ documents : "uploaded, nullable"
    users ||--o{ agent_conversations : "owns"
    agent_conversations ||--o{ agent_conversation_turns : "contains"
    documents ||--o{ processing_runs : "is processed by"
    processing_runs ||--o{ document_chunks : "produces, PDF or DOCX"
    processing_runs ||--o{ document_rows : "produces, CSV"
    document_chunks ||--o{ chunk_embeddings : "is embedded as"

    users {
        uuid id PK
        text username UK
        text role "admin or user"
        int auth_version "bumped to revoke tokens"
    }
    workspace_memberships {
        uuid user_id PK
        uuid workspace_id PK
        text role "owner, editor, or viewer"
    }
    documents {
        uuid id PK
        uuid workspace_id FK
        uuid created_by_user_id FK "null for external sources"
        text sha256
        text bucket
        text object_key
        uuid active_processing_run_id "the run whose chunks are live"
        timestamptz deleted_at "a tombstone, not a hard delete"
    }
    processing_runs {
        uuid id PK
        uuid document_id FK
        uuid workspace_id
        text job_kind "extract or delete"
        text status "12 states"
        int attempts
        timestamptz lease_until
        uuid lease_token
        timestamptz available_at
        int chunk_count "0 for a CSV"
        int row_count "0 for a document"
        jsonb csv_columns "labels each returned cell"
    }
    document_rows {
        uuid id PK
        uuid document_id FK
        uuid processing_run_id FK
        int row_number
        jsonb data "one row, verbatim"
    }
    document_chunks {
        uuid id PK
        uuid processing_run_id FK
        int ordinal
        text text
        text content_hash
    }
    chunk_embeddings {
        uuid chunk_id PK
        text embedding_profile PK
        uuid qdrant_point_id UK
        text status
    }
```

`documents.status` does not exist. It is derived from `active_processing_run_id`'s run,
in one function, which is why the document and the pipeline cannot disagree.

A document has either `document_chunks` or `document_rows`, never both, and the run
carries the count that says which: `row_count = 320` for a CSV, `chunk_count = 412` for
a PDF. `csv_columns` sits on the run rather than on every row because it labels each
returned cell, and repeating a header across 100k rows is the cost this design avoids.

`document_rows.data` is JSONB and read with `->>`, which is why a SQL error saying
`column "year" does not exist` comes back to the model as a hint naming `data->>'Year'`
rather than as a failure. The harness translates the error rather than replaying it,
because the model can fix the second query and cannot fix a bare failure.

## Development

```bash
make check     # pytest, ruff, mypy
```

`tests/test_postgres_integration.py` covers the things SQLite cannot show —
concurrent identical uploads racing on a unique constraint, foreign-key ordering, RLS
against a real `retriv_reader`, and the full CSV path from HTTP upload through storage
and the worker to a queryable row. It skips unless `TEST_POSTGRES_URL` is set.

Two tests there currently fail and are unrelated to CSV: the concurrent-upload race can
observe a rolled-back duplicate, and one assertion still expects `RUN_CHUNKING` where the
worker now sets `RUN_EMBEDDING` directly.

Tests here are checked by mutation as well as by passing. A test that stays green when
the behaviour it names is reverted is worse than no test, so the citation rules in
particular are verified by reverting each one — a per-row `chunk_id`, `section_path`
repopulated with column names, a dangling separator beside an empty locator — and
confirming the suite fails.
