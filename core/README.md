# Retriv Core

Internal document-context service for events emitted by `retriv`. It accepts signed
`document.ready` and `document.deleted` webhooks, processes PDF and DOCX files with Docling,
stores the complete structured extraction in private object storage, and stores retrieval chunks
in PostgreSQL. It generates embeddings through an OpenAI-compatible API and stores vectors in a
separate Qdrant collection.

## Data Ownership

```text
retriv
  raw S3 objects + source metadata + delivery outbox

retriv-core
  PostgreSQL: durable event inbox + processing runs + chunk text + embedding ledger
  Qdrant: vectors + identifier/hash payloads (no chunk text)
  S3: structured Docling artifacts
```

PostgreSQL remains the source of truth. Each `chunk_embeddings` row records whether the matching
chunk hash and embedding profile is pending, processing, waiting for retry, indexed, or failed.
Qdrant point IDs are deterministic, so retrying an acknowledged write safely overwrites the same
point. Qdrant payloads contain workspace, document, processing-run, and chunk identifiers plus
hash/profile metadata; they never contain chunk text.

## Processing Flow

```text
signed webhook
  -> durable inbox and queued run
  -> bounded S3 download
  -> size and SHA-256 verification
  -> Docling PDF/DOCX conversion
  -> PDF-aware Indonesian/English OCR
   -> compressed DoclingDocument artifact in S3
   -> structure-aware chunks in PostgreSQL
   -> OpenAI-compatible embedding batches
   -> vectors and identifier metadata in Qdrant
   -> active run switched atomically after every chunk is indexed
```

Raw objects must use this source key contract:

```text
workspaces/{workspace_id}/objects/{sha256}/{document_id}
```

Derived artifacts use:

```text
workspaces/{workspace_id}/documents/{document_id}/runs/{run_id}/attempts/{lease_token}/manifest.json
workspaces/{workspace_id}/documents/{document_id}/runs/{run_id}/attempts/{lease_token}/docling-document.json.gz
```

## Local Setup

Local execution requires:

- The sibling `retriv` infrastructure running MinIO on `127.0.0.1:9000`.
- Docker for PostgreSQL, Qdrant, and the document-processing worker.
- An OpenAI-compatible embedding server. Local development uses LM Studio on port `1234`.
- `uv` for the host API and development commands.

Compose runs PostgreSQL on `127.0.0.1:5433`, Qdrant on `127.0.0.1:6333`, and the worker in its
required Debian runtime. Qdrant data is persisted in the `qdrant_data` volume and requires an API
key. The internal API always runs directly on the host.

For LM Studio, load `text-embedding-qwen3-embedding-8b`, enable its OpenAI-compatible server,
allow connections from the Docker host bridge, and use a rotated API token. The worker resolves
the host as `host.docker.internal` and calls `/v1/embeddings`. The configured response dimension is
4096 and must match both the model response and Qdrant collection. Do not reuse service tokens
across LM Studio, Qdrant, MinIO, or the internal API.

LM Studio defaults to listening on `127.0.0.1` only. For a Docker worker, restart the server
with a network-visible binding (with LM Studio API-key authentication enabled):

```bash
lms server stop
lms server start --port 1234 --bind 0.0.0.0
```

This exposes LM Studio to other hosts on the local network; restrict host access accordingly.
The worker cannot reach a server bound only to `127.0.0.1` on the host.

The sibling `retriv` stack provisions the `context-service` MinIO user and private
`context-documents` bucket. Keep these values aligned between both `.env` files:

```text
retriv OUTBOX_SIGNING_KEY_ID       = internal INTERNAL_WEBHOOK_KEY_ID
retriv OUTBOX_SIGNING_SECRET       = internal INTERNAL_WEBHOOK_SECRET
retriv MINIO_INTERNAL_USER         = internal INTERNAL_S3_ACCESS_KEY
retriv MINIO_INTERNAL_PASSWORD     = internal INTERNAL_S3_SECRET_KEY
retriv MINIO_CONTEXT_BUCKET        = internal INTERNAL_S3_ARTIFACT_BUCKET
```

Create the environment file and install the host API dependencies:

```bash
cp .env.example .env
make install
```

If `.env` already exists, merge the embedding and Qdrant settings from `.env.example`. Replace
`INTERNAL_EMBEDDING_API_KEY` with a newly rotated LM Studio token and set independent
`INTERNAL_QDRANT_API_KEY` and `INTERNAL_QDRANT_READ_ONLY_API_KEY` values before starting Qdrant.
The agent uses only the latter key. The example values are placeholders.

Start the sibling infrastructure first so MinIO and its service credentials are available:

```bash
cd ../api
make infra
cd ../core
```

Build/start the Docker worker and run the API on the host:

```bash
make run
```

`make run` starts PostgreSQL and Qdrant, applies migrations, builds and starts the Docker worker,
then keeps the host API attached to the terminal. Press Ctrl+C to stop the API; Docker services
remain running. Use `make down` to stop them. Use `make api` or `make worker` to start components
separately.

The worker connects to MinIO through `INTERNAL_S3_CONTAINER_ENDPOINT_URL`. Its image installs
Tesseract with `eng` and `ind` language data and bakes the Docling layout/table models into
`/opt/docling-models`, so runtime extraction is offline and independent of the host OS.

If embedding fails after extraction, the run remains inactive and enters `retry_wait`; the worker
reuses the persisted chunks and retries only non-indexed ledger rows. A periodic reconciliation
queues existing active chunks that do not have the configured embedding profile, so deployments
can backfill without re-running Docling. Delete jobs and tombstone reconciliation remove Qdrant
points using `workspace_id` and `document_id` payload filters.

Configure the source host relay target as:

```text
OUTBOX_TARGET_URL=http://127.0.0.1:8082/internal/events
```

Run the source relay from the sibling project after both APIs are ready.

## Development Commands

Run checks without installing the large optional extraction stack:

```bash
uv sync
uv run pytest -q
uv run ruff check .
uv run mypy app worker alembic tests
```

Tests inject a deterministic extractor and in-memory object storage. The production worker loads
Docling only when the optional `extraction` dependency is installed.

## Internal API

```text
POST /internal/events
GET  /internal/workspaces/{workspace_id}/documents/{document_id}/status
GET  /internal/workspaces/{workspace_id}/documents/{document_id}/chunks
GET  /internal/workspaces/{workspace_id}/documents/{document_id}/chunks/{chunk_id}
POST /internal/processing-runs/active
POST /internal/processing-runs/recent
POST /internal/context/search
GET  /health/live
GET  /health/ready
```

The event endpoint verifies HMAC-SHA256 over the exact request bytes using:

```text
{unix_timestamp}.{event_id}.<raw-body>
```

The context and processing-run endpoints require `Authorization: Bearer <INTERNAL_API_TOKEN>`. This token
authenticates the source `retriv` service only and must never be given to a browser or agent.
`POST /internal/context/search` remains an internal API for trusted service callers; the agent's
answer flow no longer calls it. The agent uses separately provisioned read-only PostgreSQL and
Qdrant credentials, verifies a Retriv-signed workspace scope, applies the workspace/profile
filter in Qdrant, then validates active runs, indexed profile, hashes, and document state in
PostgreSQL before passing any chunk text to its LLM. API documentation is disabled when
`INTERNAL_ENVIRONMENT=production`.

The worker runs each Docling conversion in a disposable child process with wall-clock,
address-space, CPU, process-count, file-size, and container resource limits. Model access is forced
offline, credentials are removed from the parser environment, and the parent process disables
same-UID process inspection. Set deployment-level encrypted storage and network egress controls in
production.

## Format Behavior

- PDF: layout and table extraction with region-aware OCR enabled for `ind` and `eng`.
- DOCX: structural parsing for text, headings, lists, and tables.
- XLSX: event is durably accepted and recorded as `unsupported_format`; it is not retried.
- Delete events: chunks become unavailable immediately, retained source metadata is scrubbed, and
  every attempt-specific artifact under the document prefix is removed by a cleanup job. A periodic
  tombstone reconciliation repeats the prefix sweep to cover writers that fail during deletion.
