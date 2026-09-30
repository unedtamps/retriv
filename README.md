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

```
POST /v1/workspaces/{id}/documents
  └─ 202 Accepted, with the document and a queued processing run
       committed in one transaction

worker
  └─ claims the run, fetches, extracts, stores the artifact,
     chunks, embeds, upserts into Qdrant, marks the run ready

GET /v1/processing-runs/active
  └─ the dashboard polls this every 10s
```

A document cannot exist without a way to be processed: the row and the run commit
together or not at all. There is no outbox and no retry-on-replay, because there is no
network hop where an event could be lost.

Deleting is three steps for the same reason. The API marks the document deleted and
queues a cleanup run; the worker removes the Qdrant points and the artifacts and only
then deletes the row. The ordering is not incidental — the Qdrant point IDs live in the
embedding ledger, which cascades away with the document, so deleting the row first would
orphan every vector the document ever produced. Until the worker finishes, an identical
re-upload is refused with `409 document_deletion_in_progress`.

## Questions

`POST /v1/agent/ask` and the conversation routes derive the workspace scope from the
caller's memberships and call the harness directly. The `search_context` tool accepts a
query and nothing else, so a model has no argument with which to widen its scope; the
retriever filters Qdrant by workspace, re-validates every point against PostgreSQL, and
the route refuses an answer that cites anything outside the scope. A conversation turn
that cited a workspace the caller has since lost stays visible in the private transcript
but its sources are blanked and it is never replayed as context.

Greeting-only messages are answered without touching retrieval, so a user with no
workspace membership can still say hello.

## Development

```bash
make check     # pytest, ruff, mypy
```

`tests/test_postgres_integration.py` covers the two things SQLite cannot show —
concurrent identical uploads racing on a unique constraint, and foreign-key ordering. It
skips unless `TEST_POSTGRES_URL` is set.
