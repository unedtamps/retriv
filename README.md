# Retriv

Retriv is a workspace-scoped document backend. It accepts PDF, DOCX and CSV files,
extracts and indexes their contents, and exposes the result to people through a
dashboard API and to agents through an MCP server.

```
PDF / DOCX  ->  chunks + embeddings  ->  semantic search and in-order reading
CSV         ->  SQL rows             ->  aggregation and filtered queries
any file    ->  object storage       ->  signed download addresses
```

Every answer is scoped to the workspaces the caller belongs to. A document is
deduplicated by content, not by workspace: the same bytes uploaded to two
workspaces stay one object with one pipeline, one chunk set, and one vector set.

For the detailed internals — data model, pipeline stages, retrieval rules, MCP
tools, RLS and storage layout — read [`app/README.md`](app/README.md).

## Architecture

There is one API process, one worker process, one database, and the external
services the deployment provides: object storage, embeddings, and vision.

![Retriv system architecture](docs/images/retriv-ui-architecture.png)

*Two processes, one processing queue, one scoped index. Source:
[`docs/retriv-ui-architecture.pen`](docs/retriv-ui-architecture.pen); object
storage and model endpoints are external services.*

The API answers requests. The worker does the expensive, untrusted part: native
markdown extraction (PyMuPDF, python-docx and an ONNX OCR engine) in a sandboxed
subprocess, chunking, embeddings, and Qdrant writes.
They coordinate through PostgreSQL processing runs; there is no direct HTTP call
between them.

| Component | Short role |
|---|---|
| Dashboard | Workspace catalogue, uploads, members and processing status. |
| MCP client | Claude Code, Codex or a custom agent using read-only tools. |
| `app/` | Auth, document intake, scoped retrieval and the MCP server. |
| PostgreSQL | Canonical documents, workspace links, runs, chunks and CSV rows. |
| Qdrant | Candidate vectors filtered by the `workspace_ids` payload. |
| S3 / R2 | Raw uploads and extracted artifacts, one object per content hash. |
| `worker/` | Sandboxed native extraction, chunking, embeddings and payload sync. |
| ClamAV | Malware scan before any bytes reach storage. |
| LM Studio + VLM | Text embeddings and descriptions of document pictures. |

## Core concepts

### Workspace

A workspace is the unit of access. Users join through `workspace_memberships`
with one of three roles:

| Role | Can read | Upload | Download | Delete | Manage members |
|---|---:|---:|---:|---:|---:|
| `owner` | yes | yes | yes | yes | yes |
| `editor` | yes | yes | yes | yes | no |
| `viewer` | yes | no | yes | no | no |

`admin` is a platform role for user and workspace administration. It does not
grant access to workspace documents.

### Canonical document and workspace links

`documents` is one row per content hash. `document_workspaces` is the link table
that decides who can see it.

```
                 +----------------------+
                 | documents            |
                 | id (canonical)       |
                 | sha256 (unique)      |
                 | origin_workspace_id  |
                 +----------+-----------+
                            |
              +-------------+-------------+
              |                           |
    +---------v----------+     +----------v---------+
    | document_workspaces|     | processing_runs    |
    | workspace_id       |     | chunks / vectors   |
    | filename           |     | rows / artifacts   |
    | deleted_at         |     +--------------------+
    +--------------------+
```

What that means in practice:

- **One upload, one pipeline.** Uploading the same bytes to a second workspace
  creates a link; it does not upload the object again or re-run extraction.
- **One chunk set, one vector set.** Chunks and embeddings belong to the
  canonical document. They are not duplicated per workspace.
- **A filename per workspace.** The same content can be `report.pdf` in Finance
  and `q4-results.pdf` in Legal.
- **Delete is a link operation.** Removing one workspace's link leaves the
  document available to the others. The canonical row, object, chunks and
  vectors are removed only when the last link goes away.
- **One status.** The processing run is shared; a failed or ready document is
  failed or ready in every workspace that links to it.

### Processing

An upload creates a processing run. The worker claims queued runs from
PostgreSQL with a lease, fetches the raw object, extracts it, chunks and embeds
it, and marks the run ready. Failures are retried with backoff; a lease that
expires is reclaimed by the next worker. The run is the only source of document
status.

PDF and DOCX become chunks and vectors. CSV becomes rows in PostgreSQL and is
queried with SQL; it is never embedded. Pictures inside documents are described
by an optional multimodal model and the description is folded into the chunk
that held the picture.

### Retrieval

- `search_context` embeds the question, searches Qdrant inside the caller's
  workspace scope, then re-validates every returned point against PostgreSQL.
- `read_document` reads chunks by `ordinal`, which answers “what comes after
  this” where a vector search cannot.
- `query_rows` runs model-written SQL under a read-only role and PostgreSQL
  row-level security.
- `download_document` returns a short-lived signed object-store link and a
  permission-checked API URL; the bytes never pass through the model context.

The Qdrant payload carries a `workspace_ids` array. A link change queues a small
worker job that updates that array for every chunk of the document in one
Qdrant call; nothing is re-chunked or re-embedded.

## Document lifecycle at a glance

1. **Upload.** The file is staged, scanned, validated and hashed.
2. **Link or store.** If the hash already exists, the target workspace gets a
   link. Otherwise the raw object is stored once and a processing run is queued.
3. **Process.** The worker extracts, chunks and embeds PDF/DOCX, or stores CSV
   rows for SQL. One run per canonical document.
4. **Use.** Search, read in order, query CSV rows, or mint a download address.
   Every result is checked against the caller's live workspace access.
5. **Delete.** Removing a workspace link hides it only there. The last unlink
   queues cleanup of the object, chunks and vectors.

## Quickstart

### Prerequisites

- Docker Engine and Docker Compose v2.
- `uv` for local test/lint, or Python 3.12+.
- An OpenAI-compatible embedding server reachable from the worker container.
- An OpenAI-compatible multimodal endpoint for picture descriptions. Set
  `RETRIV_IMAGE_PROCESSING_ENABLED=false` to skip this stage.
- An S3-compatible object-store account. Cloudflare R2 is the reference
  provider; AWS S3 and other S3-compatible services are supported.
- About 4 GiB of RAM for ClamAV.

### Object storage

Create two buckets before starting the stack:

- `raw-documents` — original uploads;
- `context-documents` — extracted artifacts and manifests.

Create two R2 API tokens:

| Token | Permission | Buckets |
|---|---|---|
| API | `Object Read & Write` | raw only |
| Worker | `Object Read & Write` | raw + artifact |

The API token uploads and signs downloads. The worker token reads raw objects
and writes artifacts. R2 scopes permissions per bucket and cannot express
prefix-level policies; that is why the worker token is scoped to both buckets.

### Configuration and run

```bash
cp .env.example .env
# Fill in object storage, database, JWT, embedding and vision settings.
```

The example file lists every variable and the R2/AWS choices. At minimum, set
the object-store endpoint, region, both token pairs, both bucket names, and the
first admin credentials.

```bash
make worker      # build and start the extraction worker
make migrate     # start infrastructure and apply migrations
make api         # run the API on http://127.0.0.1:9000
```

The dashboard is a separate npm project:

```bash
cd web && npm ci && npm run dev
```

It runs at `http://127.0.0.1:9500`.

`make api` reuses the worker image already built. After changing worker code,
run `make worker` or `make run-build` first. Object storage is external: no
MinIO service runs locally.

## Security

- Dashboard users authenticate with a short-lived HS256 JWT; MCP clients use a
  long-lived `rk_...` API key.
- The JWT carries `auth_version`. Password rotation and bootstrap-admin changes
  bump it, revoking outstanding tokens on the next request.
- Workspace access is checked from live memberships on every request, never
  from a caller-supplied workspace id.
- API keys are read-only and reach only `/mcp` and the document download route.
- Uploaded files are scanned by ClamAV before storage. Encrypted PDFs and
  macro-enabled Office files are rejected.
- MCP search results are validated against PostgreSQL; Qdrant is a candidate
  index, not an authority.
- PDF/DOCX extraction runs in a sandboxed subprocess with memory, process,
  file-size and time limits.

## Repository map

| Path | What it is |
|---|---|
| `app/` | FastAPI API, MCP server, retrieval, storage and processing orchestration |
| `worker/` | the extraction worker process |
| `alembic/` | schema migrations |
| `web/` | the dashboard |
| `tests/` | unit and PostgreSQL integration tests |
| `docker/` | one-shot Postgres init scripts |
| `compose.yaml` | local PostgreSQL, Qdrant, ClamAV and worker |

Detailed app documentation: [`app/README.md`](app/README.md).

## Operations

- Apply schema changes with `alembic upgrade head` (or `make migrate`).
- The worker image contains a copy of `app/`; rebuild it after changing any
  shared code with `make worker`.
- The multi-workspace change uses a new Qdrant payload shape (`workspace_ids`).
  If upgrading an existing deployment, drop or reindex the Qdrant collection.
- Health: `GET /health/live` is liveness; `GET /health/ready` checks PostgreSQL,
  object storage, ClamAV and Qdrant.
- Tracing: with `RETRIV_TRACING_ENABLED=true` both processes export OTLP spans
  to the Jaeger container (`docker compose up -d jaeger`); the UI is at
  `http://localhost:16686`. One trace per request on the API, one per claimed
  run on the worker (`process.run` → `extract` → `describe` → `embed`, plus
  SQL, HTTP and S3 child spans). The extraction sandbox is never traced — it
  has no network; its cost shows in the worker's `extract` span. Set
  `RETRIV_TRACING_ENABLED=false` to turn it off.

## Development

```bash
make test     # pytest
make check    # pytest + ruff + mypy
```

Tests that need a real PostgreSQL server are skipped unless
`TEST_POSTGRES_URL` is set.
