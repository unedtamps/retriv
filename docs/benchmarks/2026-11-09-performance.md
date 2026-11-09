# Retriv performance benchmark and profile

**Date:** 2026-11-09  
**Code under test:** `2e2ab2f` — `refactor: remove no-op access job and duplicate catalogue reader` (the simplification merge), on top of `6ed3eda`  
**Environment:** local development stack: FastAPI/uvicorn API, worker container with a 4-CPU / 12 GiB cgroup, PostgreSQL 17 + pgvector 0.8.7, LM Studio embedding server, Jaeger  
**Corpus:** 9 ready documents, 2,691 chunks, 4.1 MB of extracted text, 2,900 rows, 79 images, one workspace

Raw measurements are in [`docs/benchmarks/2026-11-09/`](2026-11-09/) and linked at the end of this report.

> **Implementation status (2026-11-09):** P0-1, P0-2, P0-5 and P1-1 from the
> [improvement backlog](2026-11-09-improvements.md) are implemented in this branch. The P1-2
> embedding-batch recommendation was **withdrawn** after a controlled re-test showed the
> original comparison was confounded by input length. See
> [§9](#9-applied-fixes-2026-11-09) for the post-change verification and the correction.

---

## TL;DR

**This is not a Python-language problem.** On the measured stack:

- **67% of a `search_context` call is the embedding model** (72.3 ms of 107.9 ms end-to-end).
- **18% is the vector SQL**, and that query is currently doing a full scan + sort instead of using the HNSW index.
- Only **about 6% is the Python/protocol layer** (formatting, validation, JSON-RPC, auth, tracing). Python CPU inside the retrieval tool is ~6.2 ms per call.
- **Extraction is ~50% ONNX, ~12% PyMuPDF, and the rest native/IO.** The process runs at 3.9 CPU-seconds per wall-second; the GIL is not the limit.
- The API routes answer in **3–5 ms**. The worker and the retrieval model are the cost centres.

The highest-value improvements are query shape, model/batch configuration, and a couple of missing indexes — not a rewrite:

| # | Finding | Measured effect | Priority |
|---|---|---|---|
| 1 | Vector search scans all embeddings and sorts them; HNSW is not used | 15.9–19.5 ms → **0.55–2.8 ms** when made nearest-first (6–29×) | P0 |
| 2 | **Withdrawn:** the apparent batch-16 slowdown was an input-length confound, not batching | Same 16 texts: **2.121 s batched vs 2.140 s as 16 single calls** (~1%); keep batch 16 | — |
| 2b | The 8B embedding model is the real ingestion cost | `nomic-embed-text-v1.5` embedded the same 16 chunks in **76.7 ms vs 2.12 s** (~28×), subject to retrieval-quality evaluation | P1 |
| 3 | `search_text` uses `ILIKE '%…%'` with no trigram index | **21 ms** for 4.1 MB; grows linearly (~5 s/GB) | P1 |
| 4 | Section completion fetches every chunk under each anchor, then keeps 4 | 54 fetched for 4 kept; rewrite with a SQL row limit | P1 |
| 5 | The simplification moved catalogue paging from SQL to Python | 10,000 entries ≈ **10.9 ms of Python** plus the full DB transfer, on every page | P1 |
| 6 | `tiktoken` cold start downloads/loads BPE in the sandbox | **2.4–3.3 s** on a cold worker, plus a network dependency | P1 |
| 7 | In-container parallel parsers do not add throughput | 2 parsers: 2.34 p/s vs 2.45 p/s serial; +0.7 GB RAM | P2 |

The simplification itself is behaviourally sound and the test suite passes (**588 passed, 18 skipped**). It removes one queue hop per workspace-link change. The one performance regression it introduced is catalogue pagination, which now materializes the whole catalogue before slicing.

---

## 1. Scope and method

### What was measured

| Path | What runs | Why it matters |
|---|---|---|
| `search_context` | embed query → pgvector search → section completion → format | the main agent question path |
| `search_text` | literal `ILIKE` over chunk text | identifier / exact-string questions |
| `read_document`, `get_document_outline`, `describe_document` | ordinal window / section metadata / run stats | navigation and diagnostics |
| `find_documents`, `list_queryable_files`, `list_workspaces` | metadata queries | the "which file?" door |
| `query_rows` | model-authored SQL over JSONB rows via the read-only role | CSV / workbook questions |
| HTTP `/v1` routes | dashboard API | separate from MCP, same database |
| extraction | sandboxed PyMuPDF + pymupdf4llm layout/table ONNX + OCR | ingestion CPU and memory |

### Method

- **Component timing:** the same `ContextRetriever` and `AgentToolKit` objects the API uses, on the live database, with a warm embedding connection. Each stage was timed separately and as a composite.
- **End-to-end MCP:** a persistent streamable-HTTP session against the running API on `:9000`, one warm call then five measured calls per tool.
- **Tracing comparison:** a second API instance with `RETRIV_TRACING_ENABLED=false` on `:9101` to isolate OpenTelemetry overhead.
- **Database plans:** `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` on the exact retrieval, completion, text-search and catalogue statements.
- **Extraction:** the repository's own `scripts/benchmark_extraction.py` inside the worker image, with `--cpus 4 --memory 12g` to mirror the deployed cgroup.
- **Python profile:** `cProfile` around the real sandbox runner on a 30-page sample.
- **Narrative timing:** Jaeger’s OTLP traces from the live API, which expose the embedding HTTP span and each SQL span inside one `tools/call`.

### Caveats

- This is a **small development corpus** (2,691 chunks). The absolute SQL numbers are small; the scaling arguments are projections, labelled as such.
- The embedding endpoint is **LM Studio on a laptop RTX 4060 (8 GB)**. Batch behaviour is specific to that server/model combination; verify it on the production endpoint.
- `cProfile` roughly doubles CPU-bound runtime, so use its **relative shares**, not its absolute total.
- The API was already running; it had a warm process, warm database cache, and warm embedding connection. The extraction benchmarks use cold sandbox processes, as production does per document.
- No production-like load generator was used. Concurrency was measured with 24 in-flight retrievals at 1/4/8/16 workers.

---

## 2. What the code is

### Shape

```
                    ┌──────────────────────────────┐
  JWT  /v1/*  ─────▶│  FastAPI API process         │
                    │  auth · uploads · status     │
  API key /mcp ────▶│  MCP server (read-only)      │
                    │  AgentToolKit · harness      │
                    └──────────────┬───────────────┘
                                   │  PostgreSQL
                    ┌──────────────▼───────────────┐
                    │  documents · links · runs    │
                    │  chunks · embeddings (HNSW)  │
                    │  rows (JSONB) · images       │
                    └──────────────▲───────────────┘
                                   │  lease / SKIP LOCKED
                    ┌──────────────┴───────────────┐
                    │  worker process (separate)   │
                    │  sandboxed extraction        │
                    │  chunk · embed · describe    │
                    └──────────────────────────────┘
```

- **One API process** serves the dashboard (`/v1`, JWT) and the MCP server (`/mcp`, `rk_…` API key). The two surfaces share `AgentToolKit`, the retrievers, and the permission checks.
- **One worker process** claims processing runs from PostgreSQL with a lease and `FOR UPDATE SKIP LOCKED`. It extracts in a **sandboxed subprocess** per document, then writes chunks, rows, embeddings and image artifacts.
- **One database** is the source of truth: canonical documents, workspace links, processing runs, chunks, pgvector embeddings, JSONB rows, API keys and refresh tokens.
- **Object storage** holds the raw bytes and extracted artifacts; ClamAV scans before storage.
- **The agent path is read-only.** Upload, delete and status are HTTP/dashboard operations. MCP tools never write.

### Retrieval model

- `documents` is one row per content hash; `document_workspaces` is the access link. The scope is always the caller’s live memberships, never a client-supplied workspace list.
- `chunk_embeddings` is a `vector(2000)` column with an HNSW cosine index. The search joins the chunk, the active run, the live document and a workspace link, and validates the chunk’s content hash.
- `section_key` is the JSON-encoded ancestor heading path. A search that lands on a heading completes the section by matching the key itself or its subtree (`text_pattern_ops` index).
- `ordinal` is the chunk’s reading position in the run. `read_document` windows over `(processing_run_id, ordinal)`; `get_document_outline` scans positions and section keys.
- `document_rows` stores CSV/XLSX cells as JSONB. Model-authored SQL runs as a dedicated read-only role with RLS, a statement timeout and an allowlist.

### Modules and size

| Area | Lines | Role |
|---|---:|---|
| `app/extraction/` | 3,706 | PyMuPDF/pymupdf4llm, OCR, layout, DOCX/XLSX, chunking, images |
| `app/processing/` | 2,567 | run queue, worker loop, pipeline stages |
| `app/agents/` | 2,002 | in-server agent loop, tool specs, `AgentToolKit` |
| `app/retrieval/` | 1,940 | vector search, section completion, navigation, SQL rows, formatting |
| `app/routes/` | 1,770 | `/v1` HTTP routes |
| `app/documents/` | 1,225 | upload validation, ingest, dedup, delete |
| `app/mcp/` | 912 | MCP tools, auth, runtime, documents |
| `app/security/` | 760 | JWT, refresh, API keys, authorization |
| `app/api/` | 678 | dependencies and response schemas |
| `app/db/` | 574 | SQLAlchemy models and session |
| `app/infrastructure/` | 485 | S3 and ClamAV |
| `app/core/` | 456 | settings, errors, logging |
| `app/vision/` | 380 | VLM client |
| `app/http/` | 203 | upload body limit |
| `app/embeddings/` | 160 | OpenAI-compatible embedding client, pgvector helper |
| `app/observability/` | 87 | OpenTelemetry setup |

Total Python: **18,171 lines in `app/`**, 130 in `worker/`, 2,129 in `scripts/`, 16,714 in `tests/`. The web app is 12,681 TypeScript/TSX lines.

The largest single files are `app/processing/worker.py` (**2,389 lines**), `app/agents/harness.py` (763), `app/agents/toolkit.py` (755), `app/extraction/xlsx_reader.py` (734), `app/retrieval/context.py` (654), `app/routes/admin.py` (635), `app/documents/validation.py` (583), `app/retrieval/sql.py` (556), `app/db/models.py` (544) and `app/mcp/server.py` (496). `worker.py` is the main maintainability hotspot: it owns extraction orchestration, vision, embedding, row storage, cleanup and reconciliation.

### The simplification merge

`2e2ab2f` changes 16 files, **+314 / −387 lines**:

- **Removes the `access` processing job.** A workspace link change was previously acknowledged by a queued `access` run. Retrieval already reads the link live, so the job was a no-op. Link create/delete now just commit the row. This removes one `processing_runs` insert and one worker claim per duplicate upload, link or delete.
- **Collapses the two catalogue readers into one.** `CatalogueReader` now owns paging and filename search over the application session; `RowQuery` only executes model-authored SQL. This removes the hand-written JSONB catalogue statement (and the duplicate code path when row queries are disabled). It also changes `list_queryable_files` to behave identically in both deployments.
- **Renames/removes the `RUN_SYNCING` state** and narrows the `job_kind` constraint in migration `0002_drop_access_jobs.py`.
- **Uses the detected media type rather than the filename** for the “queryable with `query_rows`” hint, so a `.csv` that failed validation does not claim to be queryable and a workbook does.

The access-job removal is a write-path simplification: it removes one row insert and one worker queue hop per duplicate upload/link/delete. We did not measure a material read-path difference from it; it is strictly less work, not a new cost. The trade-off is in the catalogue reader: **paging used to happen in SQL with `LIMIT/OFFSET`; the merged version fetches all matching documents, expands their sheets in Python, then slices.** At 9 documents this is invisible (0.67 ms). At 10,000 row-stored files it is measurable (see Finding 6).

---

## 3. End-to-end measurements

### 3.1 The search path

Measured against the live corpus with the production defaults:

```
limit=8 matches, max_context_sources=12, section_expand_chunks=4,
min_relevance_score=0.25, embedding=text-embedding-qwen3-embedding-8b (2000 dims)
```

| Stage | p50 | p95 | Share of full retrieve |
|---|---:|---:|---:|
| Embedding HTTP request | 45.72 ms | 68.24 ms | ~66% |
| Vector SQL (`SEARCH_VECTORS`) | 17.66 ms | 19.44 ms | ~25% |
| Section completion SQL | 6.78 ms | 7.58 ms | ~10% |
| **Full `retrieve()`** | **69.33 ms** | **84.03 ms** | 100% |
| `AgentToolKit.search_context` (format + hint) | 67.87 ms | 84.63 ms | — |
| MCP `search_context` (persistent session) | 98.13 ms | 102.73 ms | — |

Components were timed in isolation, so the shares sum slightly above 100% because of run-to-run variance. The MCP number includes the first-call/warm-up variability; the trace below is the cleanest decomposition.

**Jaeger trace of one MCP `search_context` (107.85 ms):**

| Span | Duration | Share |
|---|---:|---:|
| Embedding `POST /v1/embeddings` | 72.31 ms | 67.0% |
| Vector SQL | 19.47 ms | 18.1% |
| Section completion SQL | 7.40 ms | 6.9% |
| API-key + user + membership auth | 0.42 ms | 0.4% |
| HTTP connection setup | 1.80 ms | 1.7% |
| MCP framing, formatting, validation, tracing | ~6.45 ms | ~6.0% |
| **Total `tools/call search_context`** | **107.85 ms** | 100% |

This is the single most important chart in the report: **the Python/protocol layer is ~6 ms; the model is ~72 ms; the database is ~27 ms.**

### 3.2 Concurrency

24 full retrievals per scenario, warm pool, same six queries:

| In-flight | Wall | Throughput | Mean latency |
|---:|---:|---:|---:|
| 1 | 1.746 s | 13.75 calls/s | 72.7 ms |
| 4 | 1.143 s | 21.00 calls/s | 47.6 ms |
| 8 | 1.145 s | 20.97 calls/s | 47.7 ms |
| 16 | 1.140 s | 21.06 calls/s | 47.5 ms |

Throughput **plateaus at ~21 searches/s** with four or more in flight. The API process and the database are not saturated; the embedding server is. Adding API replicas without adding embedding capacity will not raise this ceiling.

### 3.3 MCP tools

Persistent streamable-HTTP session, 5 measured calls each after a warm-up. “In-process” is the same toolkit method without JSON-RPC/auth (from the component harness).

| Tool | MCP p50 | In-process p50 | Fixed MCP overhead | Notes |
|---|---:|---:|---:|---|
| `search_context` | 98.13 ms | 67.87 ms | ~6 ms protocol + model variance | embedding dominates |
| `search_text` | 31.81 ms | 24.72 ms | ~7 ms | 25.9 ms is the SQL |
| `read_document` | 10.83 ms | 4.39 ms | ~6 ms | ordinal window |
| `get_document_outline` | 9.94 ms | 4.97 ms | ~5 ms | 1,059 positions scanned |
| `describe_document` | 13.10 ms | 5.75 ms | ~7 ms | 6 sequential queries |
| `find_documents` | 5.86 ms | 0.99 ms | ~5 ms | 1.7 ms SQL |
| `list_queryable_files` | 5.30 ms | 0.67 ms | ~4.6 ms | catalogue |
| `list_workspaces` | 5.90 ms | 0.81 ms | ~5 ms | 2 queries |
| `query_rows` (count) | 4.99 ms | 0.71 ms | ~4.3 ms | row SQL + link lookup |
| `get_image` | not measured | — | — | storage-bound |
| `download_document` | not measured | — | — | S3 presign |

The fixed MCP overhead is **~4–7 ms per call**, of which ~1.1 ms is authentication (endpoint middleware 0.6 ms + per-tool re-auth 0.4–0.8 ms) and the rest is JSON-RPC/SSE, tracing, formatting and event-loop scheduling. For metadata tools whose actual work is ~1 ms, this overhead is larger than the work, but it is not a large absolute cost. Tracing adds roughly **1–4 ms** per call; with tracing disabled, cheap tools measured 3.8–8.8 ms.

`search_text` deserves a callout: 25.9 ms of its 31.8 ms is a single SQL statement (see Finding 3).

### 3.4 HTTP API

Dashboard routes with a valid JWT (10 measured requests each):

| Route | p50 | p95 |
|---|---:|---:|
| `GET /health/live` | 0.99 ms | 1.04 ms |
| `GET /health/ready` | **139.24 ms** | 150.81 ms |
| `GET /v1/workspaces` | 4.16 ms | 4.93 ms |
| `GET /v1/workspaces/{id}/documents` | 5.44 ms | 6.68 ms |
| `GET /v1/workspaces/{id}/documents/{doc}` | 4.76 ms | 5.76 ms |
| `GET /v1/documents/search?q=report` | 3.56 ms | 3.91 ms |
| `GET /v1/documents/search?q=pdf&limit=20` | 5.03 ms | 6.30 ms |
| `GET /v1/processing-runs` | 3.45 ms | 4.49 ms |
| `GET /v1/api-keys` | 2.59 ms | 2.83 ms |

The HTTP surface is not a bottleneck. Readiness is 139 ms because it performs a **live S3 `HeadBucket`** (`S3.HeadBucket` spans in Jaeger measured 118–172 ms), plus `pg_isready`-style SQL and a ClamAV `PING`. If the readiness probe is frequent, this is a needless external round trip; caching it for a few seconds would cut it to ~1 ms.

### 3.5 Extraction

The repository’s own benchmark, inside the worker image with `--cpus 4 --memory 12g`:

| Scenario | Pages | Wall | Throughput | CPU-seconds | CPU/wall | Peak RSS |
|---|---:|---:|---:|---:|---:|---:|
| 1 parser | 410 | 167.36 s | 2.45 pages/s | 653.08 | 3.90× | 1.18 GB |
| 2 parsers | 546 | 233.38 s | 2.34 pages/s | 919.09 | 3.94× | 1.88 GB |

For the same 546 pages, serial extraction at the measured 2.45 pages/s would take **~222.9 s**; two parallel parsers took **233.4 s** — **4.7% slower**, with **+0.70 GB** of peak memory. On a 4-CPU container, adding parsers trades memory for latency, not throughput. The deployment already gets capacity the right way: `docker compose up -d --scale worker=N`. The project documentation’s three-container run (956 pages in 225 s, 4.25 pages/s) is consistent with this.

Per-document fixed cost: a 5-page sample takes **~3.0 s** warm, a 30-page sample **~10.6 s**, and a 410-page filing **167 s**. The first ~1.5–2.5 s is process/import/model startup; for small documents that is a large fraction of the total.

### 3.6 Ingestion embedding — corrected

The first batch measurement compared a single short query (batch 1) with 16 mixed-length chunks (batch 16). That compares different inputs, not different batching. A controlled test on the **same 16 texts**:

| Grouping | Requests | Total p50 | Per text |
|---|---:|---:|---:|
| 16 in one request | 1 | 2.121 s | 132.6 ms |
| 8 + 8 | 2 | 2.124 s | 132.8 ms |
| 4 × 4 | 4 | 2.109 s | 131.8 ms |
| 8 × 2 | 8 | 2.133 s | 133.3 ms |
| 16 × 1 | 16 | 2.140 s | 133.7 ms |

Request grouping is **neutral** (~1% difference, in favour of the larger batch). LM Studio logs one “Returning embeddings” line per input rather than one per request, and the server takes ~2.1 s for this text set either way. The earlier table’s “batch 1 = 43–66 ms” was the first (short) chunk, while “batch 16 = 182 ms/chunk” came from 16 longer chunks; it was not an apples-to-apples comparison.

What actually drives the cost is **input length**:

| Input characters | One-text p50 |
|---:|---:|
| 5 | 37.1 ms |
| 100 | 44.2 ms |
| 300 | 62.0 ms |
| 665 | 118.3 ms |
| 1,000 | 150.5 ms |
| 2,000 | 288.3 ms |
| 4,000 | 567.8 ms |
| 8,000 | 1,179.0 ms |

The fit is roughly **35 ms fixed + 0.14 ms/char**. The 16-text controlled set averaged 665 chars/text, which predicts ~128 ms/text and matches the measured 132.6 ms. The corpus average is ~1,526 chars/chunk, predicting ~250 ms/chunk and therefore roughly **11 minutes for 2,691 chunks** — not the 1.9 minutes the first comparison implied. The model’s loaded context is 2,048 tokens, so longer inputs are also subject to server-side truncation; validate that against the pipeline’s chunk sizes.

The real lever is **model choice**, not batch size. `nomic-embed-text-v1.5` (768 dims) embedded the same 16 medium chunks in **76.7 ms (4.8 ms/text)** versus **2.12 s (132.6 ms/text)** for the 8B model — about **28×** on this set. That is a retrieval-quality trade-off, not a free win, but it is far larger than anything batch tuning can do. Shortening chunks reduces the per-char term linearly; the batch setting stays at 16 because it is marginally faster and uses 16× fewer requests.

A full 60-query English retrieval comparison is in
[`2026-11-09-embedding-comparison.md`](2026-11-09-embedding-comparison.md): nomic with its
required prefixes reached 75% recall@8 versus Qwen3’s 95%, while Qwen3 truncated to 768
dimensions stayed at 93.3% — the storage/index win without the model swap.

### 3.7 Row queries

On 2,900 rows via the read-only role:

| Query | p50 |
|---|---:|
| `count(*)` | 1.66 ms |
| `group by Customer, sum(Amount)` | 2.34 ms |
| filtered + ordered + `limit 20` | 0.88 ms |

The row path is fast at this size and is not a current concern.

---

## 4. Profiling: where the time actually goes

### 4.1 Vector search plan — the main database finding

The current `SEARCH_VECTORS` statement is a single query with joins and an `ORDER BY embedding <=> :query LIMIT :limit`. PostgreSQL’s plan on the 2,691-row corpus:

```
Limit (actual 8 rows, 16.07 ms)
  Sort (top-N heapsort, 26 kB)
    Nested Loop (2,691 rows)
      Nested Loop (2,691 rows)
        Nested Loop (2,691 rows)
          Seq Scan on documents (9 rows)
          Bitmap Heap Scan on chunk_embeddings (299 rows × 9)
          Index Scan on document_workspaces (2,691 loops)
      Index Scan on document_chunks (2,691 loops)
```

It **scans every embedding and sorts them**. The HNSW index is present but the counter showed only **14 scans** (including our forced test queries), versus 5,543 scans of the dispatch index. The planner estimates a full scan is cheaper at 2,691 rows, and at this size it is not catastrophically wrong — but it is also not using the index that exists for exactly this query.

Measured alternatives:

| Plan | Execution time |
|---|---:|
| Current full scan + sort | **15.9–19.5 ms** |
| Full query forced to HNSW (`enable_seqscan=off; enable_bitmapscan=off; enable_sort=off`) | **2.83 ms** |
| Two-stage nearest-first CTE, default planner | 9.8 ms |
| Two-stage nearest-first CTE, forced to HNSW | **0.55 ms** |
| Simple HNSW `ORDER BY … LIMIT 8` (warm) | **0.47 ms** |

The two-stage shape is:

```sql
WITH nearest AS (
    SELECT chunk_id, embedding <=> :query AS distance
    FROM chunk_embeddings
    WHERE embedding_profile = :profile
      AND model = :model
      AND dimensions = :dimensions
      AND status = 'indexed'
      AND embedding IS NOT NULL
    ORDER BY embedding <=> :query
    LIMIT :oversample          -- e.g. limit * 4, to survive scope/run filters
)
SELECT 1 - nearest.distance AS score, chunk.*, link.workspace_id, link.filename
FROM nearest
JOIN document_chunks AS chunk ON chunk.id = nearest.chunk_id
JOIN documents AS document ON document.id = chunk.document_id
JOIN document_workspaces AS link ON ...
WHERE document.deleted_at IS NULL
  AND document.active_processing_run_id = chunk.processing_run_id
  AND link.workspace_id IN (:workspace_ids)
  AND link.deleted_at IS NULL
ORDER BY nearest.distance
LIMIT :limit;
```

The important part is **nearest-first**: the approximate index produces the candidate list before the scope/active-run joins, instead of scanning and sorting the whole vector set. This is the difference between O(N) and O(log N)-ish behaviour. It must be over-sampled because a nearest candidate can be filtered out by scope or a superseded run; validate recall on `evals/retrieval_golden.json` before shipping.

**Index size:** 40 MB of HNSW for 2,691 vectors (~14.9 KB/vector), plus 21 MB of TOAST for the 2000-dimensional vectors and 1 MB of heap. The whole `chunk_embeddings` relation is 63 MB. At 1 million chunks that is ~**23 GB**, of which ~15 GB is the HNSW graph. A `halfvec(2000)` index would roughly halve it; a 1024-dimension `halfvec` index would roughly quarter it. That is a real deployment constraint, not a micro-optimization.

A direct 768-dimension Qwen3 measurement (same vectors, truncated with `subvector`, same HNSW options) is in
[`2026-11-09-embedding-comparison.md`](2026-11-09-embedding-comparison.md): the current corpus’s
relation falls **63 MB → 21 MB** and the HNSW index **40 MB → 10 MB**, with a 1.7 pp
recall@8 change on the 60-query set.

### 4.2 Section completion — fetches all descendants

For the largest section in the corpus (54 chunks), `_complete_sections` fetched **54 rows and kept 4** (the configured `section_expand_chunks`), taking **4.43 ms in SQL** and **12.5 ms** through the Python method (including validation and hashing). The plan loops over all 2,691 chunks in the run:

```
Sort (54 rows)
  Nested Loop (54 rows)
    Nested Loop (2,691 rows)
      ...
      Index Scan on document_chunks (2,691 loops, 1 row removed by filter)
```

The method has no SQL `LIMIT`: the `section_expand_chunks` cap is applied in Python after the rows arrive. At one million chunks the same shape is a ~1.6 s scan per search, to return four rows. A `ROW_NUMBER() OVER (PARTITION BY anchor ORDER BY ordinal)` or a `LATERAL` per anchor with `LIMIT width` would keep the transfer proportional to the answer.

### 4.3 Literal text search — no trigram index

`search_text` compiles to `chunk.text ILIKE '%needle%'` plus the same scope/run validation. On 4.1 MB of chunk text it takes **21.2 ms** and the plan is a bitmap/sequential scan of `document_chunks`:

```
Limit (21.2 ms)
  Sort
    Nested Loop
      Nested Loop
        Seq Scan on documents (9 rows)
        Bitmap Heap Scan on document_chunks (6 rows per doc, 293 removed by filter)
```

There is no `pg_trgm` extension and no trigram index. Cost is essentially **5.1 ms per MB of text** (21 ms / 4.1 MB), so 100 MB is ~0.5 s and 1 GB is ~5 s. A GIN trigram index turns this from a scan into an index lookup:

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX ix_document_chunks_text_trgm
    ON document_chunks USING gin (text gin_trgm_ops);
```

`ILIKE '%…%'` is supported by `gin_trgm_ops`; the index adds write cost at ingestion and size on disk, but `search_text` is an exact-string tool and this is its natural access method.

### 4.4 Catalogue — paging moved to Python

The merged `CatalogueReader.queryable_files` runs one query for **all** matching row-stored documents, expands every workbook sheet in Python, then slices `entries[offset:offset+limit]`. The query itself is 0.03 ms for the two row-stored files; the Python expansion at synthetic sizes is:

| Rows in catalogue | Python build + slice |
|---:|---:|
| 3 | 0.00 ms |
| 100 | 0.09 ms |
| 1,000 | 0.95 ms |
| 10,000 | **10.9 ms** |

That is before the database transfer of all 10,000 `csv_columns` JSON values and the memory for the expanded entries. The pre-merge path did the same work in SQL with `LIMIT/OFFSET` and a window count (`count(*) over ()`), so the transferred and materialized set was one page. Restoring SQL paging is a small change with a clear scaling win.

### 4.5 Extraction profile

`cProfile` on a 30-page sample (22.23 s under the profiler, so relative only):

| Function / span | Self/cumulative time | Share |
|---|---:|---:|
| `onnxruntime.InferenceSession.run` | 11.02 s | 49.5% |
| `pymupdf.page_get_textpage` | 2.77 s | 12.4% |
| `tiktoken.load.read_file` / network | 2.40 s | 10.8% |
| TLS socket read (tiktoken download) | 1.62 s | 7.3% |
| ONNX session creation | 0.37 s | 1.7% |
| `pymupdf.layout` / table grid / image features | most of the ONNX time | — |
| Python glue (`numpy`, `startswith`, `getattr`, regex, list appends) | < 2 s combined | < 9% |

The two dominant blocks are **native code**: the layout/table ONNX models and PyMuPDF’s text extraction. The CPU/wall ratio of **3.9×** confirms that the work is threaded inside those native libraries; the GIL is not serializing it. Python orchestration is a small fraction.

`pymupdf4llm.to_markdown(..., table_strategy="lines_strict")` is applied to every text page, which is why the table/layout models run even on prose. A content-aware gate (only run table detection on pages with ruling lines or dense tabular text) would cut extraction time on narrative reports; it is a quality/engineering trade-off, not a language issue.

### 4.6 Sandbox startup and the tiktoken cold start

A cold sandbox process pays:

- **~0.75 s** to import `app.extraction.native` (PyMuPDF, layout/ONNX imports, numpy).
- **~0.07 s** to load the tokenizer when the BPE cache is warm; **~2.5 s** when it is cold. The first cold sandbox in our container downloaded a 1.7 MB `cl100k_base` file over HTTPS.

Because sandbox subprocesses share the worker’s `/tmp`, a long-running worker warms `/tmp/data-gym-cache` after its first document and subsequent documents pay only ~0.1 s. The real issues are:

1. **A cold worker (or a fresh container) pays 2.4–3.3 s and needs network egress.**
2. **The sandbox’s environment allowlist does not include `TIKTOKEN_CACHE_DIR`**, even though it passes `TMPDIR`/`XDG_CACHE_HOME`. Setting the cache directory in the worker is not enough unless the key is added to `SAFE_ENVIRONMENT_KEYS`.
3. **The documentation says the extraction sandbox has no network**, but the profile shows it does: `tiktoken` performed an HTTPS download. In a truly network-isolated deployment the first extraction would fail. Baking the BPE cache into the worker image (and passing its path) removes both the cold-start cost and the dependency.

Measured cold vs warm tokenizer (same 4-CPU container):

| Document | Cold cache | Warm cache | Saved |
|---|---:|---:|---:|
| 5 pages | 6.36 s | 3.08 s | 3.28 s |
| 30 pages | 13.10 s | 10.67 s | 2.43 s |

The warm running worker measured 3.0–3.5 s for 5 pages and 10.5–10.7 s for 30 pages, confirming the cache is shared after the first document.

### 4.7 Memory

| Process / scenario | RSS |
|---|---:|
| API process (idle) | 184.8 MB |
| Worker container (idle) | 130.9 MB |
| PostgreSQL container | 77.5 MB |
| One 410-page extraction | **1.18 GB peak** |
| Two parallel extractions | **1.88 GB peak** |
| MCP formatting of 12 × 3 KB sources | 76 KB peak |

The API process is small. The worker’s peak is driven by the native parser/ONNX models, not Python objects. Two parsers add 0.70 GB for no throughput gain on four CPUs.

### 4.8 MCP fixed overhead

From Jaeger traces of cheap tools:

- Endpoint API-key auth: ~0.6 ms (key lookup + user).
- Per-tool re-auth: ~0.4–0.8 ms (key + user + membership scope).
- JSON-RPC/SSE framing, tracing, formatting, event loop: ~2–4 ms.
- Total: **~4–7 ms per call**.

That is the cost of the deliberate “re-authenticate on every tool call” security rule. It is acceptable at current volumes; a short-lived principal cache would save ~1 ms per call at the cost of delaying revocation by the cache TTL. Not a priority.

---

## 5. Findings and recommendations

### P0 — do first

#### 1. Make the vector search nearest-first so it uses HNSW

**Evidence:** 15.9–19.5 ms today; 2.83 ms forced HNSW on the full query; 0.55 ms for a two-stage CTE forced to HNSW; 0.47 ms for a simple HNSW scan. `pg_stat_user_indexes` shows the HNSW index used 14 times (forced tests) versus 5,543 scans of the dispatch index. At 2,691 vectors the plan scans and top-N sorts all of them.

**Action:** rewrite `SEARCH_VECTORS` as a nearest-first CTE/`LATERAL` with an over-sample, join the scope/run validation after the candidate list, and verify the plan uses `ix_chunk_embeddings_hnsw`. If the planner still prefers the sort at small sizes, set `enable_sort = off` for the retrieval session (one query per session) or introduce a partial/covering index. Re-run `scripts/eval_retrieval.py` to confirm recall; over-sampling must cover candidates filtered by workspace/active run.

**Expected:** 6–29× faster vector SQL on this corpus; avoids a linear scan becoming seconds at 100k+ chunks.

**Risk:** medium — filtered approximate search can miss the requested `limit` if too many nearest candidates are stale; over-sample and validate.

#### 2. Withdrawn: embedding batch size is not the lever

**Correction:** the original “batch 1 vs batch 16” comparison was confounded by input length — the batch-1 sample was one short chunk, while the batch-16 sample was 16 longer chunks. A controlled re-test on the **same 16 texts** shows **2.121 s as one batch vs 2.140 s as 16 single requests** (~1%, in favour of the batch). Request grouping is neutral; LM Studio processes the inputs one by one.

**Evidence:** per-input cost scales with text length at roughly **35 ms + 0.14 ms/char** (5 chars 37 ms; 665 chars 118 ms; 2,000 chars 288 ms; 8,000 chars 1,179 ms). The corpus average is ~1,526 chars/chunk, which predicts ~250 ms/chunk and roughly **11 minutes for 2,691 chunks**, not 1.9 minutes.

**Action:** keep `RETRIV_EMBEDDING_BATCH_SIZE=16` (fewer HTTP requests, marginally faster). If ingestion latency matters, evaluate a smaller embedding model — `nomic-embed-text-v1.5` embedded the same 16 medium chunks in **76.7 ms vs 2.12 s** (~28×) — or shorten chunks, and validate retrieval quality with `scripts/eval_retrieval.py`.

**Risk:** model change is a quality trade-off; chunk-length changes affect retrieval granularity. Batch-size tuning has no meaningful upside here.

### P1 — high value, small/medium effort

#### 3. Add a trigram index for `search_text`

**Evidence:** 21.2 ms for 4.1 MB of text, no `pg_trgm`; cost grows ~5 ms/MB. `search_text` is an exact-string tool, so a GIN trigram index is the right access method.

**Action:** migration with `CREATE EXTENSION IF NOT EXISTS pg_trgm` and `CREATE INDEX … USING gin (text gin_trgm_ops)`. Check index size and ingestion cost; a partial index on the active pipeline/run is an option if old runs dominate.

**Expected:** sub-millisecond to a few milliseconds for exact strings at current size; avoids seconds at GB-scale text.

#### 4. Bound section completion in SQL

**Evidence:** 54 rows fetched for 4 kept; 4.43 ms SQL / 12.5 ms method for the largest section; the query loops all 2,691 chunks. The Python cap is applied after transfer.

**Action:** `ROW_NUMBER() OVER (PARTITION BY anchor ORDER BY ordinal)` and filter `rn <= section_expand_chunks`, or a `LATERAL` join per anchor with `LIMIT`. Keep the same validation predicates and re-run the completion tests (the attribution-by-`section_key` logic must still hold).

**Expected:** transfer proportional to the configured width (4) instead of the section size (54 here, potentially thousands); less memory and hashing.

#### 5. Restore SQL paging in the catalogue reader

**Evidence:** the merged reader fetches all matching documents, expands all sheets in Python, then slices. 10,000 entries cost 10.9 ms of Python plus the full DB transfer; the pre-merge query used `LIMIT/OFFSET` with a window count.

**Action:** keep the unified `CatalogueReader` (the simplification is good), but move the page slice back into SQL. A `count(*) OVER ()` plus `LIMIT/OFFSET` on the normalized sheet rows (the old `catalogue_statement`) is enough, or fetch one page of documents and expand their sheets with a bounded page. Keep the `pattern` filter in SQL.

**Expected:** constant work per page instead of O(all row-stored documents); restores the scaling property the simplification removed.

#### 6. Bake the tiktoken cache and pass it into the sandbox

**Evidence:** cold 5-page sandbox 6.36 s vs 3.08 s warm; cold 30-page 13.10 s vs 10.67 s warm; 1.7 MB download; `TIKTOKEN_CACHE_DIR` is not in `SAFE_ENVIRONMENT_KEYS`; the sandbox is not actually network-isolated.

**Action:** in the worker image, set `TIKTOKEN_CACHE_DIR=/app/.cache/tiktoken` and run `python -c 'import tiktoken; tiktoken.get_encoding("cl100k_base")'` at build time. Add `TIKTOKEN_CACHE_DIR` to `SAFE_ENVIRONMENT_KEYS` and set it in the compose worker environment. This makes extraction deterministic on a network-isolated host.

**Expected:** removes 2.4–3.3 s from the first document per container; removes a runtime network dependency.

### P2 — scaling and architecture

#### 7. Scale extraction by worker containers, not in-container parsers

**Evidence:** 2 parsers: 2.34 pages/s vs 2.45 pages/s with 1, +0.70 GB RSS, 4.7% slower for 546 pages. CPU/wall is 3.9× on 4 CPUs; a second parser competes for the same cores.

**Action:** keep `RETRIV_PARSER_MAX_CONCURRENT_EXTRACTIONS=1` for 4-CPU containers; scale with `--scale worker=N` (the project already documents 3 containers → 4.25 pages/s). Revisit only if containers get more CPUs or the ONNX thread cap is tuned down. Derive the hard-coded `_ONNX_THREAD_CAP = 4` and `MKL/OMP/OPENBLAS=4` from the cgroup CPU quota so a 2-CPU deployment does not oversubscribe.

#### 8. Reduce vector memory: `halfvec` and/or fewer dimensions

**Evidence:** HNSW index 40 MB for 2,691 vectors (~14.9 KB/vector); TOAST 21 MB; total relation 63 MB. At 1M chunks that is ~23 GB for vectors + graph. The model is Matryoshka-trained and already truncated from 4096 to 2000 dims.

**Action:** evaluate `halfvec(2000)` (2× reduction) and `halfvec(1024)` (4×), with `scripts/eval_retrieval.py` before/after. pgvector 0.8 supports halfvec HNSW indexes. This is a deployment-scale decision, not a quick patch.

#### 9. Cache query embeddings for repeated queries

**Evidence:** 72.3 ms of a 107.9 ms MCP search is the embedding call. The same query repeated by a retrying agent pays it again. A small LRU/TTL cache keyed by `(model, normalized query)` would return the cached vector; the remaining work is ~35.5 ms (3.0× faster).

**Action:** add an LRU (e.g., 1,024 entries, 10–15 min TTL) in `ContextRetriever` or a shared cache; invalidate on model/profile change. Queries are immutable text, so staleness is not a correctness issue; memory is bounded. This does not help unique questions, but agent retries and repeated user questions are common.

#### 10. Consider a shorter embedding model for latency-sensitive deployments

**Evidence:** `nomic-embed-text-v1.5` answered a query in 4.4 ms vs 39.8–72.3 ms for qwen3-8B, and embedded 18 texts in 40 ms (2.2 ms/text). That is ~9× on the query path and ~17× on ingestion. It is a quality trade-off, not a drop-in win.

**Action:** run `scripts/eval_retrieval.py` with the smaller model, re-index a copy, and compare recall/MRR. If quality holds for the corpus, the latency and GPU-memory wins are large. If not, keep the 8B model and use the batching/caching fixes above.

### P3 — smaller items

- **Cache readiness for a few seconds.** `GET /health/ready` is 139 ms because of a live S3 `HeadBucket` (118–172 ms). A 5–10 s TTL cache keeps readiness honest without an external round trip on every probe.
- **MCP principal cache.** ~1 ms per call; a 1–5 s per-key cache would save it, at the cost of a short revocation delay. Only worth it at high tool-call volume.
- **Batch the `describe_document` counts.** It runs six sequential queries (scoped document, workspace, run, chunk count, image count, described count) plus an outline scan; 5.75 ms in-process, 13.1 ms MCP. Two aggregate queries could halve it. Low impact.
- **Split `worker.py`.** 2,389 lines is the maintainability hotspot; extraction, vision, embedding, rows, cleanup and reconciliation in one module. Runtime-neutral, but it makes the next performance change harder.
- **Gate ONNX table detection on narrative pages.** Most extraction time is table/layout ONNX. A page-level heuristic (ruling lines / dense tabular fragments) could skip it on prose-only pages. Quality risk; measure on the extraction corpus.

---

## 6. Is this a language issue?

**No — at least not in the sense of “Python is too slow for this workload.”**

The measured evidence:

| Path | Python CPU | Wall | Python share |
|---|---:|---:|---:|
| `AgentToolKit.search_context` | 6.21 ms/call | 71.9 ms | 8.6% |
| MCP `search_context` (trace) | ~6.5 ms (formatting, validation, protocol) | 107.9 ms | ~6% |
| `format_context` (12 × 3 KB) | 0.047 ms | — | — |
| `principal_from_headers` | 0.84 ms | — | — |
| API document list | ~1–2 ms of Python (estimated) | 5.4 ms | ~30% |
| Extraction | small glue (<9% of profiled time) | 167 s for 410 pages | native code dominates |

The wall-clock is spent on:

1. **The embedding model** — 67% of search. This is GPU inference in LM Studio, not Python. A Go/Rust API calling the same model would wait the same 72 ms.
2. **PostgreSQL** — 25% of search, dominated by the full vector scan/sort. This is a query-plan/index problem, language-independent.
3. **ONNX/PyMuPDF** — ~62% of extraction. These are C/C++ libraries called through Python bindings; the CPU/wall ratio of 3.9× shows they already use multiple cores. Rewriting the orchestration in Go/Rust would still call the same native libraries (or reimplement them).
4. **Process startup** — ~1.5–2.5 s per sandbox, mostly imports and model session setup. This is an architecture choice (one sandbox per document), not a language tax; a pre-warmed process pool would amortize it in any language.

Where Python does cost something measurable:

- **Vector response parsing and validation:** the 6.2 ms CPU per search includes decoding 2,000 floats, normalizing, Pydantic validation and SHA-256 hashing. At 21 searches/s this is ~13% of one core; at 100+ searches/s it would matter, and multiple uvicorn workers are the answer.
- **MCP framing/auth:** ~5 ms per call, ~1 ms of which is auth; this is protocol and security design, not interpreter speed.
- **Sandbox startup:** imports and `tiktoken` are Python-level fixed costs; caching/pre-warming removes most of them.
- **GIL:** the API is async and mostly waiting on IO, so the GIL is not the limit. The worker puts extraction in a subprocess and the native libraries release the GIL; the GIL is not the limit there either.

**When would a rewrite make sense?** If profiling showed >30–40% of wall time in pure-Python CPU (not native calls, not IO), or if a single process were GIL-bound at the target QPS. Neither is true here: the API uses ~6 ms CPU per 108 ms search, and extraction is native-bound. A rewrite would add FFI/packaging complexity for PyMuPDF/ONNX and would not remove the model or database costs.

**What to do instead:** fix the query shape (HNSW), the embedding batch size, the missing trigram index, the unbounded section/catalogue fetches, and the cold-start cache. Those are the measured wins.

---

## 7. Scale projection

These are **indicative linear projections** from the measured 2,691-chunk corpus, not measurements at scale. They assume the current query shapes and one embedding profile.

| Resource | 2,691 chunks (today) | 100k chunks | 1M chunks |
|---|---:|---:|---:|
| Vector SQL, current full scan + sort | 15.9–19.5 ms | ~0.6–0.7 s | ~6–7 s |
| Vector SQL, nearest-first HNSW | 0.55–2.8 ms | ~1–3 ms | ~2–5 ms (graph depth grows slowly) |
| HNSW index size | 40 MB | ~1.5 GB | ~15 GB |
| Vector TOAST + heap | 22 MB | ~0.8 GB | ~8 GB |
| Total vector relation | 63 MB | ~2.3 GB | ~23 GB |
| `search_text` (`ILIKE`, no trigram) | 21 ms | ~0.5 s (100 MB) | ~5 s (1 GB) |
| Section completion (scans all chunks) | 4.4 ms | ~0.16 s | ~1.6 s |
| Catalogue Python expansion (10k files) | 10.9 ms | ~109 ms at 100k | ~1.1 s at 1M |

The first row is the one that turns a working deployment into a slow one. The HNSW rewrite and the embedding batch setting are the two changes with the largest effect per unit of effort.

---

## 8. Reproduce

### Extraction

```bash
# Inside the worker image, with the deployment's 4-CPU / 12 GiB cgroup:
docker run --rm --cpus 4 --memory 12g \
  -v "$PWD/app:/app/app:ro" -v "$PWD/scripts:/app/scripts:ro" \
  -v /path/to/data:/data:ro \
  retriv-api-worker /app/.venv/bin/python /app/scripts/benchmark_extraction.py \
  --parallel 1 --json /data/corp-10k-2025.pdf
```

For the profile, run the sandbox runner under `cProfile` with the same arguments and mount a writable output directory:

```bash
/app/.venv/bin/python -m cProfile -o /out/extract.prof \
  -m app.extraction.sandbox_runner \
  --input /data/annualreport-30p.pdf --media-type application/pdf \
  --output-dir /out/out --chunk-max-tokens 700 --tokenizer-name cl100k_base \
  --scan-text-threshold 32 --no-scan-page-describe \
  --timeout-seconds 3600 --memory-limit-bytes 4294967296 --max-processes 4096 \
  --max-artifact-bytes 104857600 --max-chunk-payload-bytes 104857600 \
  --max-extracted-text-bytes 52428800 --max-chunks 10000 \
  --image-render-scale 2.0 --image-min-pixels 10000 \
  --max-image-bytes 5242880 --max-total-image-bytes 52428800
```

### Database plans

The exact statements are in `app/retrieval/context.py` (`SEARCH_VECTORS`, `_complete_sections`, `search_text`) and `app/retrieval/sql.py` (`CatalogueReader.queryable_files`). Prefix the rendered SQL with:

```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) <statement>;
```

Set `SET LOCAL enable_seqscan=off; SET LOCAL enable_bitmapscan=off; SET LOCAL enable_sort=off` in separate statements to see the HNSW plan; a single multi-statement string is rejected by asyncpg’s prepared-statement protocol.

### MCP and HTTP

The MCP harness used a persistent `streamable_http_client` session with `Authorization: Bearer rk_…`, one warm call, then five measured calls. The HTTP harness used a signed JWT for a workspace owner and ten measured GETs per route. The raw outputs are linked below.

### Tests

```bash
uv sync --extra extraction
uv run pytest -q
# 588 passed, 18 skipped, 12 warnings in 66.28s
```

The optional extraction dependencies are required to collect the worker/extraction tests; a base install fails collection on `openpyxl`.

---

## 9. Applied fixes (2026-11-09)

This section records what changed after the measurements above, so the report is not read as a
list of untested recommendations.

### 9.1 Vector search now uses HNSW

`app/retrieval/context.py` now runs a **materialized nearest-first CTE**:

- the candidate list is produced by the HNSW index before the scope/document joins;
- the workspace `EXISTS` test is inside the CTE, so candidates are in scope;
- `candidate_limit = max(limit * retrieval_candidate_multiplier, 32)` (default multiplier 8);
- the retrieval transaction sets `jit=off`, `enable_sort=off`, `enable_seqscan=off`,
  `enable_bitmapscan=off` and `hnsw.ef_search = candidate_limit`.

The planner settings are transaction-local and affect only the one retrieval query. The
`MATERIALIZED` fence is required: without it, PostgreSQL inlined the CTE and produced a
~732 ms plan in the prototype instead of the ordered index path.

Post-change measurement on the same corpus (see `retrieval-after.json`):

| Stage | Before | After |
|---|---:|---:|
| Embedding HTTP | 45.72 ms | 43.18 ms |
| Vector SQL | **17.66 ms** | **3.38 ms** |
| Section completion SQL | 6.78 ms | 7.08 ms |
| Full `retrieve()` | 69.33 ms | **57.98 ms** |
| `AgentToolKit.search_context` | 67.87 ms | **52.50 ms** |

The vector stage is ~5.2× faster; the end-to-end search improvement is smaller because the
embedding model varies between runs and now dominates even more. Concurrency stayed at
~21 searches/s for 4+ in flight, confirming the embedding server remains the ceiling.

### 9.2 Embedding-batch recommendation withdrawn

The original report compared a single short query with 16 mixed-length chunks. A controlled
re-test on the same 16 texts measured **2.121 s as one batch vs 2.140 s as 16 single
requests** (~1%, in favour of the batch). The apparent 2.7–4.2× win was an input-length
confound, not batching. The code and compose default remain **16**.

The actual cost driver is input length (~35 ms + 0.14 ms/char), and the real lever is model
choice: the same 16 chunks took **76.7 ms** with `nomic-embed-text-v1.5` vs **2.12 s** with
the 8B model (~28×), subject to a retrieval-quality evaluation.

### 9.3 MCP error translation

`app/mcp/server.py` `_guarded` now translates:

- `RetrievalUnavailable` → `ToolError("Search is temporarily unavailable: …")`
- `DocumentNotFound` / `DocumentNotNavigable` → `ToolError(str(exc))`

so an unknown document, a CSV/workbook passed to `read_document`, or an embedding/SQL outage
is a model-readable tool result instead of an MCP crash. Tests were added for all four paths.

### 9.4 Admin deletion and reindex guards

- `DELETE /v1/admin/users/{id}` now refuses to delete the only owner of any workspace
  (`409 last_owner_required`) and locks the relevant workspace rows first. The existing test
  was extended to prove the refusal and the successful path after a second owner is assigned.
- `accept_document_reindex` now requires a live workspace link, so an owner of a workspace
  that deleted its link cannot reindex the canonical document shared with other workspaces.

### 9.5 Test status

- `uv run pytest -q`: **595 passed, 18 skipped** (4 warnings).
- `make test-postgres`: **16 passed** against `retriv_test` with pgvector and the baseline +
  `0002` migrations.
- `uv run ruff check .`: clean.
- `uv run mypy app worker alembic tests`: clean (125 source files).

### 9.6 Not done in this pass

The rest of the [improvement backlog](2026-11-09-improvements.md) remains: the `query_rows`
allowlist/cross-file fixes (P0-3/P0-4), plaintext model key (P0-6), trigram index (P1-3),
section-completion limit (P1-4), catalogue paging (P1-5), XLSX/CSV memory fixes (P1-6/P1-7),
backfill set-based rewrite (P1-8), tiktoken cache (P1-9), readiness/scope/rate-limit/JWT
items (P1-10 to P1-15), and the P2 maintainability/observability work.

---

## Appendix: raw data

| File | Contents |
|---|---|
| [`retrieval.json`](2026-11-09/retrieval.json) | component p50/p95, per-tool in-process timings, section completion shape, concurrency (before the fixes) |
| [`retrieval-after.json`](2026-11-09/retrieval-after.json) | the same harness after the nearest-first vector rewrite and batch-default change |
| [`mcp.json`](2026-11-09/mcp.json) | MCP tools with tracing enabled (the `:9000` API) |
| [`mcp-tracing-off.json`](2026-11-09/mcp-tracing-off.json) | MCP tools with `RETRIV_TRACING_ENABLED=false` (the `:9101` API) |
| [`api.json`](2026-11-09/api.json) | HTTP route timings |
| [`extraction.json`](2026-11-09/extraction.json) | extraction batches, warm/cold sandbox timings, cProfile summary, controlled embedding batch/length data |
| [`embedding-model-comparison.json`](2026-11-09/embedding-model-comparison.json) | Qwen3 vs nomic retrieval metrics, per-query ranks, bootstrap intervals, speed |
| [`golden-current.json`](2026-11-09/golden-current.json) | the 60 fresh English questions used for the embedding comparison |

### Trace used for the search breakdown

The Jaeger trace `ad19d8fe5c0b989c5b481632690931c1` contains a `tools/call search_context` with 19 spans: API-key/user/membership auth (0.42 ms), `POST /v1/embeddings` (72.31 ms), vector SQL (19.47 ms), section completion SQL (7.40 ms), plus connection and MCP framing spans. The trace ID is included for reproducibility; it contains no key material or document text.

### Corpus snapshot

| Metric | Value |
|---|---:|
| Documents | 9 |
| Chunks | 2,691 |
| Extracted text | 4,106,437 chars |
| Average tokens/chunk | 335 |
| Max tokens/chunk | 966 |
| Rows (`document_rows`) | 2,900 |
| Images (`document_images`) | 79 |
| Workspaces | 1 |
| Database size | 80 MB |
| `chunk_embeddings` heap / TOAST / HNSW | 1 MB / 21 MB / 40 MB |

### What was not measured

- Upload throughput end-to-end (ClamAV + S3 + queue + extraction + embedding); only its extraction and embedding components were measured separately.
- LLM chat-completion latency for the in-browser agent loop; it is model-bound, and the tool calls inside it use the same `AgentToolKit` paths measured here.
- Vision/VLM description latency and cost; the corpus has 79 images but the VLM endpoint was not benchmarked.
- PDF/DOCX/PPTX download throughput; object storage was not on the hot path for these tests.
- Production multi-tenant load; the corpus is one workspace.
- `halfvec`/lower-dimension quality; only the current 2000-dim profile was measured.
