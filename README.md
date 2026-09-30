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
        O["OpenRouter"]
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
- `PDF` and `DOCX` are indexed. `XLSX` is stored and retrievable as a file but has no
  text to embed, so its run ends immediately in `unsupported_format` rather than sitting
  on "processing" forever.
- A document's status is derived from its active processing run. There is no status
  column, so the document and the pipeline cannot disagree.

## Prerequisites

- Docker Engine and Docker Compose v2.
- `uv` for local test/lint, or Python 3.12+.
- An OpenAI-compatible embedding server on `127.0.0.1:1234` that is reachable from the
  worker container.
- An OpenRouter key for answers.
- About 4 GiB of RAM for ClamAV on first startup.

## Running locally

```bash
cp .env.example .env
```

Fill in every `CHANGE ME`. The only one that cannot be generated for you is the OpenRouter
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

    U->>W: choose a PDF
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
    K->>K: Docling extraction in a sandboxed subprocess
    K->>M: PUT the artifact and its manifest
    K->>DB: INSERT document_chunks
    loop in batches
        K->>L: POST /embeddings
        L-->>K: vectors
        K->>Q: upsert points, each payload carrying its workspace and run
    end
    K->>DB: set status=ready, document.active_processing_run_id = run
```

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
    queued --> unsupported_format: media type has no text to embed
    fetching --> extracting
    extracting --> storing_artifact
    storing_artifact --> chunking
    chunking --> embedding
    embedding --> ready: all vectors upserted
    queued --> cancelled: document deleted first
    fetching --> retry_wait: transient failure
    extracting --> retry_wait: transient failure
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

## Asking a question

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant A as app
    participant DB as PostgreSQL
    participant H as harness
    participant L as OpenRouter
    participant E as LM Studio
    participant Q as Qdrant

    U->>A: POST /v1/agent/ask
    A->>DB: SELECT workspace_ids from memberships<br/>where role has search_context
    alt the user is in no searchable workspace
        A-->>U: 403 context_access_required
    end
    A->>H: answer(question, workspace_ids)

    loop at most 2 tool calls, 3 model requests
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
    processing_runs ||--o{ document_chunks : "produces"
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
        text status "11 states"
        int attempts
        timestamptz lease_until
        uuid lease_token
        timestamptz available_at
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

## Development

```bash
make check     # pytest, ruff, mypy
```

`tests/test_postgres_integration.py` covers the two things SQLite cannot show —
concurrent identical uploads racing on a unique constraint, and foreign-key ordering. It
skips unless `TEST_POSTGRES_URL` is set.
