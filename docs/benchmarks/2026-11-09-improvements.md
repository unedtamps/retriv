# Retriv improvement backlog

**Date:** 2026-11-09  
**Scope:** backend (`app/`, `worker/`, `alembic/`, tests, deployment); the web app is mentioned only where it shares a contract.  
**Basis:** the performance measurements in [`2026-11-09-performance.md`](2026-11-09-performance.md), plus a code review of the merged tip `2e2ab2f`.

> **Status (2026-11-09):** P0-1, P0-2, P0-5 and P1-1 are implemented in this branch, and the
> workbook half of P2-8 was fixed alongside P0-1. **P1-2 was withdrawn:** a controlled re-test
> showed the original batch-1/batch-16 comparison was confounded by input length; batch size is
> neutral. See
> [`2026-11-09-performance.md#9-applied-fixes-2026-11-09`](2026-11-09-performance.md#9-applied-fixes-2026-11-09)
> for the post-change verification and the correction. The remaining items below are still open.

This is a prioritized list, not a rewrite plan. The short version:

- **P0** items are correctness, security, or user-facing error handling. Fix these before scaling.
- **P1** items are measured performance/memory wins or reliability gaps.
- **P2** items are maintainability, observability, and developer experience.

Effort: **XS** < 1 hour · **S** ~half day · **M** ~1–3 days · **L** > 3 days.

---

## P0 — correctness and security

### P0-1. MCP `read_document` and search tools crash instead of returning a tool error

**Where:** `app/agents/toolkit.py:280-303`, `app/mcp/server.py:109-129`, `app/agents/harness.py:725-731`

`AgentToolKit.read_document` calls `navigator.read_window` without catching `DocumentNotFound` or `DocumentNotNavigable`. `search_context`/`search_text` likewise let `RetrievalUnavailable` escape. The MCP `_guarded` wrapper catches `ToolRejection`, `AppError`, `ToolError` and `McpNotReadyError`, but **not** those exceptions. The in-server harness catches them and feeds the model a readable result; an external MCP client gets an internal error instead.

**Fix:** translate `RetrievalUnavailable`, `DocumentNotFound` and `DocumentNotNavigable` in the toolkit methods (or add them to `_guarded`) so the message is model-readable. Add `tests/test_mcp.py` cases for: unknown document id, `read_document` on a CSV/workbook, and an embedding/SQL failure.

**Effort:** S · **Impact:** removes a common agent failure mode.

### P0-2. `DELETE /v1/admin/users/{id}` can leave a workspace ownerless

**Where:** `app/routes/admin.py:143-175`; compare `ensure_not_last_owner` at `app/routes/admin.py:387-408`

The admin delete route deletes the user and lets memberships cascade. It does not call `ensure_not_last_owner`. The existing test `test_admin_can_delete_user_and_memberships_repoint` actually deletes the sole owner of `dave-room` and leaves only a viewer. The admin member-removal route correctly refuses the same operation with `409 last_owner_required`.

**Fix:** before deleting, lock the user's owner memberships and either transfer ownership to the acting admin or refuse with 409 when a workspace would have no owner. Update the test to assert the new outcome.

**Effort:** S · **Impact:** prevents unmanageable workspaces.

### P0-3. The `query_rows` SQL allowlist is regex-based and bypassable

**Where:** `app/retrieval/sql.py:70-72` (`_TABLE_REFERENCE`), `app/retrieval/sql.py:436-448` (`_validate`)

The regex only catches bare table names after `FROM`/`JOIN`. These pass validation:

```sql
select * from document_rows, processing_runs
select * from "processing_runs"
select * from document_rows, pg_catalog.pg_tables
```

The last one reaches a public catalog view; the first two reach RLS-protected tables that the allowlist says are not readable. RLS still limits tenant rows, but the “only allowlisted tables” guarantee is false and the blast radius is larger than the code claims.

**Fix:** parse with a real Postgres parser (`pglast` or `sqlglot`) and reject any relation outside the allowlist, including comma joins, quoted names and schema-qualified names. Alternatively, reject `,` in the FROM clause and quoted relations before executing, but a parser is the durable fix. Add adversarial tests for each bypass.

**Effort:** M · **Impact:** restores the stated security boundary for model-authored SQL.

### P0-4. `query_rows` allows cross-file aggregates

**Where:** `app/retrieval/sql.py:157-180` (`_require_one_document`), `app/retrieval/sql.py:450-486` (`_to_results`)

`_require_one_document` checks only `result.sources` (citable rows). A result with no required columns goes into `values`, and aggregates are never checked for the file they came from. This passes both checks:

```sql
select count(*) from document_rows where document_id is not null
```

`mentions_document_id` only checks that the string appears. The query can aggregate across every file in the workspace, contrary to the tool description and the “one file at a time” contract.

**Fix:** require a single document scope for the statement (e.g. a `document_id = '<uuid>'` equality predicate, or a `document_id` column in the result) and reject an aggregate whose scope cannot be proven. Add tests with a two-file fixture.

**Effort:** M · **Impact:** prevents confidently wrong cross-file answers.

### P0-5. Reindex can target a workspace whose link is deleted

**Where:** `app/api/dependencies.py:313-328` (`get_owner_document`), `app/documents/service.py:208-248` (`accept_document_reindex`)

`load_scoped_document` does not filter `DocumentWorkspace.deleted_at`, and `accept_document_reindex` checks only the canonical `Document.deleted_at`. An owner of a workspace that deleted its link can therefore reindex the canonical document shared with other workspaces — changing the chunks/embeddings those other workspaces read.

**Fix:** require a live link in the named workspace for reindex (and ideally for metadata that is presented as live). Add a test: delete the link, then owner reindex returns 404/409.

**Effort:** S · **Impact:** closes an authorization gap on a shared object.

### P0-6. The chat-model API key is stored in plaintext

**Where:** `app/db/models.py:512-533` (`AgentModelSetting.api_key`), `app/routes/admin.py:486-630`

The model key is stored as typed in the database. The admin API returns only the last four characters and never logs the key, but a database read (backup, replica, SQL injection elsewhere) exposes a live provider credential. The model comment acknowledges “the database is the only copy”; that is the risk, not a mitigation.

**Fix:** encrypt at rest with an application key from the environment/KMS (Fernet or AES-GCM), or store a secret-manager reference instead of the secret. Add a migration and a rotation path; keep the preview-only response.

**Effort:** M–L · **Impact:** limits credential exposure from a DB compromise.

---

## P1 — measured performance, memory, and reliability

The measured evidence and query plans are in [`2026-11-09-performance.md`](2026-11-09-performance.md). The list below is the actionable summary.

### P1-1. Make the vector search nearest-first so it uses HNSW

**Where:** `app/retrieval/context.py:44-121` (`SEARCH_VECTORS`)

Current plan scans all 2,691 embeddings and top-N sorts them. Measured **15.9–19.5 ms**; forced HNSW **2.83 ms**; two-stage nearest-first CTE forced to HNSW **0.55 ms**. Without this, a 100× corpus becomes a multi-second search. Over-sample because scope/active-run filters can discard candidates; validate recall with `scripts/eval_retrieval.py`.

**Effort:** M · **Impact:** 6–29× on the vector stage; the biggest scalability fix.

### P1-2. Withdrawn — do not tune embedding batch size

**Where:** `app/processing/worker.py:1673-1706`, `app/core/config.py:136`

The original claim (182 ms/text at batch 16 vs 43–66 ms/text at batch 1) compared one short
chunk with 16 longer chunks. On the **same 16 texts**, one batch of 16 took **2.121 s** and 16
single requests took **2.140 s** (~1%, in favour of the batch). Request grouping is neutral;
LM Studio processes the inputs one by one.

The per-input cost is input length: roughly **35 ms + 0.14 ms/char** (2,000 chars ≈ 288 ms;
8,000 chars ≈ 1.18 s). The real lever is **model choice** — `nomic-embed-text-v1.5` is 24–36× faster but, in a
60-query English comparison, reached only 75% recall@8 versus Qwen3’s 95%. Qwen3 truncated
to 768 dimensions stayed at 93.3%, so that is the preferred storage/index optimisation.
See [`2026-11-09-embedding-comparison.md`](2026-11-09-embedding-comparison.md). Keep the
batch at 16 to reduce HTTP requests.

**Effort:** — · **Impact:** none from batch tuning; model choice is a quality trade-off.

### P1-3. `search_text` needs a trigram index

**Where:** `app/retrieval/context.py:312-345` (`search_text`)

`ILIKE '%needle%'` over 4.1 MB measured **21.2 ms**, with no `pg_trgm` extension or GIN index. Cost is ~5 ms/MB, so 1 GB is ~5 s. Add:

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX ix_document_chunks_text_trgm
    ON document_chunks USING gin (text gin_trgm_ops);
```

**Effort:** S · **Impact:** exact-string lookup from a scan to an index lookup.

### P1-4. Bound section completion in SQL

**Where:** `app/retrieval/context.py:507-650`

The largest section in the corpus: **54 rows fetched, 4 kept**; 4.43 ms SQL and 12.5 ms through the method, scanning all 2,691 chunks. `section_expand_chunks` is applied in Python after transfer. Use `ROW_NUMBER() OVER (PARTITION BY anchor ORDER BY ordinal)` with `rn <= width`, or a `LATERAL` per anchor with `LIMIT`.

**Effort:** M · **Impact:** transfer proportional to the answer, not the section size.

### P1-5. Restore SQL paging in the catalogue reader

**Where:** `app/retrieval/sql.py:209-271` (`CatalogueReader.queryable_files`)

The simplification moved paging to Python: the reader fetches every matching row-stored document, expands every sheet, then slices. At 10,000 entries the Python build alone is **10.9 ms**, before the DB transfer and memory. The pre-merge path used `LIMIT/OFFSET` with a window count. Keep the unified reader; restore SQL paging.

**Effort:** S · **Impact:** removes the one measured regression from the simplification.

### P1-6. XLSX `_stream_sheet` materializes the entire sheet

**Where:** `app/extraction/xlsx_reader.py:604-635`

```python
for row in [*buffered[start:], *source]:
```

`source` is an openpyxl row iterator; the unpack loads **every row** into a list before the `max_rows` check can fire. This defeats `read_only=True`, contradicts the docstring, and lets a large workbook consume memory up to the cap after the fact.

**Fix:** `for row in itertools.chain(buffered[start:], source):` and keep the row cap in the loop.

**Effort:** XS · **Impact:** large memory/DoS reduction on the workbook path.

### P1-7. CSV validation reads and splits the whole file in memory

**Where:** `app/documents/validation.py:174-232` (`validate_csv`), `app/documents/validation.py:236-252` (`sniff_delimiter`), `app/processing/worker.py:1340-1353` (`_decode_csv`)

`raw = path.read_bytes()` then `raw.decode(...)`, and `sniff_delimiter` calls `text.splitlines()` for the whole file. A 100 MB CSV can add ~200 MB+ to the API process and another copy in the worker. The upload limit is 100 MiB (`.env.example:76`), so this is reachable.

**Fix:** stream decode/validate from disk (incremental UTF-8 decoder + `csv.reader` over a text stream), and sample only the first N lines for delimiter sniffing. Share the helper between validator and worker.

**Effort:** M · **Impact:** bounds memory on a user-controlled input.

### P1-8. `_queue_embedding_backfill` is an N+1 reconciliation loop

**Where:** `app/processing/worker.py:567-660`

For each of up to 100 runs it loads all chunks and all embeddings, then loops in Python to insert/update one row at a time. At 10k chunks × 100 runs this is a large startup/reconciliation spike. Use a set-based `INSERT ... SELECT ... ON CONFLICT` for missing rows and `UPDATE ... FROM (VALUES ...)` for mismatched ones; keep the lease lock and batch limits.

**Effort:** M · **Impact:** worker startup and 5-minute reconciliation become bounded.

### P1-9. Bake the tiktoken cache and pass it into the sandbox

**Where:** `app/extraction/sandbox.py:23-34` (`SAFE_ENVIRONMENT_KEYS`), `Dockerfile` worker stage

A cold sandbox downloads/loads the 1.7 MB `cl100k_base` BPE file: **2.4–3.3 s** on the first document per container, and a network dependency. The running worker warms `/tmp/data-gym-cache` after the first document, but a restart pays it again and a network-isolated host fails. `TIKTOKEN_CACHE_DIR` is not in the allowlist.

**Fix:** bake the cache at image build (`TIKTOKEN_CACHE_DIR=/app/.cache/tiktoken` + `tiktoken.get_encoding("cl100k_base")`), add the variable to `SAFE_ENVIRONMENT_KEYS`, and set it in compose.

**Effort:** S · **Impact:** removes cold-start cost and a hidden egress requirement.

### P1-10. Readiness should check the embedding endpoint and cache the S3 probe

**Where:** `app/routes/health.py:30-48`

`/health/ready` measured **139 ms** because it performs a live S3 `HeadBucket` (118–172 ms) plus ClamAV and SQL. It does **not** check the embedding service — the dependency every search needs. The API can report ready while search is down, and it hits S3 on every probe.

**Fix:** add a lightweight embedding health check (model list or one-token embed) and cache the external checks for 5–10 s (or split liveness/readiness by capability).

**Effort:** S · **Impact:** readiness reflects actual search ability; probes stop paying a network round trip.

### P1-11. `MAX_SCOPE_WORKSPACES` is dead code; the scope is unbounded

**Where:** `app/retrieval/scope.py:19-34`

`MAX_SCOPE_WORKSPACES = 1000` and the docstring says “the cap stays”, but `searchable_workspace_ids` returns every membership and never applies it. A user in thousands of workspaces produces a huge `IN` list for every vector/text query. `readable_workspace_ids` in `app/routes/processing_runs.py` duplicates the same gap.

**Fix:** enforce the cap (raise a model-readable error) or paginate/partition the scope. Centralize both helpers.

**Effort:** S · **Impact:** bounds the query shape and protects the vector search.

### P1-12. Rate limiting is per-process and absent on MCP/API-key paths

**Where:** `app/security/rate_limit.py`

`LoginRateLimiter` is an in-memory `dict` guarded by an `asyncio.Lock`. With multiple API replicas, an attacker gets `max_attempts × replicas`; the key is `client_host:username`, which behind a proxy may be the proxy for every user. MCP calls and API-key auth have no rate limit at all.

**Fix:** move login limiting to a shared store (Redis/Postgres) or the gateway, add a per-API-key/per-IP limiter for `/mcp`, and configure trusted proxy headers / `X-Forwarded-For` handling.

**Effort:** M · **Impact:** brute-force and abuse resistance in a real deployment.

### P1-13. Production can start with a random JWT secret

**Where:** `app/core/config.py:35`, `app/core/config.py:343-459` (`validate_settings`)

`jwt_secret` defaults to `token_urlsafe(48)`, which is 64 chars and passes the production length check. A production deployment that forgets `RETRIV_JWT_SECRET` starts successfully, but every replica has a different secret and every restart invalidates all sessions.

**Fix:** make the secret explicitly required for `component=api` in production (sentinel/no default), or reject the generated default. Add a startup test.

**Effort:** S · **Impact:** prevents a silent, hard-to-diagnose auth failure.

### P1-14. The agent chat has no overall run deadline or token budget

**Where:** `app/agents/harness.py:625-780` (`run_agent`), `app/agents/chat.py:56` (180 s per turn)

A run can make up to 12 model turns; each provider call has a 180 s timeout, so a slow model can hold an SSE connection for up to ~36 minutes, and tool results can add up to 20k characters per call. The worker has a processing timeout; the chat path does not.

**Fix:** add a per-run wall-clock deadline and a total tool-result/token budget; close the provider and emit a retryable error on expiry.

**Effort:** S · **Impact:** bounded resource use per chat request.

### P1-15. `enqueue_delete` misses retry-wait runs

**Where:** `app/processing/service.py:107-160`

The query that prevents a duplicate delete checks `status in (queued, *ACTIVE_RUN_STATUSES)`, which excludes `retry_wait`; the cancellation update also excludes `retry_wait`. A delete run waiting to retry can therefore be duplicated, and an extract run in retry-wait is not cancelled when the document is deleted.

**Fix:** include `RUN_RETRY_WAIT` in both the existing-delete check and the cancellation update; add a test that queues a retry-wait run.

**Effort:** S · **Impact:** avoids duplicate cleanup and wasted extraction.

---

## P2 — maintainability, observability, developer experience

### P2-1. Centralize the tool catalogue and dispatch

**Where:** `app/mcp/server.py:144-496` (`build_mcp_server`, 353 lines), `app/agents/harness.py:50-339` (`_tool_specs`, 289 lines), `app/agents/harness.py:374-500` (`_execute_tool`)

The same 12 tools are declared three times: Pydantic/`@mcp.tool` for MCP, hand-written JSON Schema for the harness, and a name-based dispatcher. They match today; they will drift. A single registry (name, description, argument model, handler) can build both surfaces and dispatch by handler, removing ~600 lines and one class of bugs.

**Effort:** M · **Impact:** high maintainability; prevents schema/dispatch drift.

### P2-2. Centralize the retrieval validation predicates and link columns

**Where:** `app/retrieval/context.py` (`SEARCH_VECTORS`, `_complete_sections`, `search_text`), `app/retrieval/navigation.py` (`_window_query`, `outline`), `app/routes/processing_runs.py` (`_link_columns`)

The same servable-chunk invariants (live document, active run, matching content hash, indexed embedding, workspace link) and the same two scalar subqueries for `workspace_id`/`filename` are repeated in five places. A new invariant must be added to all of them. Build a shared `servable_chunk_conditions()`/`workspace_link_columns()` and reuse it.

**Effort:** M · **Impact:** correctness by construction; less duplication.

### P2-3. Replace positional row indexing in retrieval with mappings

**Where:** `app/retrieval/context.py:472-505` (`_text_sources`), `app/retrieval/context.py:609-650` (`_complete_sections`), `app/retrieval/navigation.py:_to_window`

Rows are read as `row[0]`, `row[1]`, etc. A select-order change breaks the code silently (wrong field becomes `filename`, etc.). Use `result.mappings()` / `.mappings().all()` and named keys. This is a small, low-risk refactor with a large readability gain.

**Effort:** S · **Impact:** removes a fragile coupling between SQL and Python.

### P2-4. Split `worker.py`

**Where:** `app/processing/worker.py` (2,389 lines)

The worker owns queue/lease, extraction, PPTX conversion, vision, embeddings, CSV/XLSX rows, cleanup, reconciliation and failure handling. The largest functions are `_process_extract` (185 lines), `_process_xlsx_extract` (151), `_process_csv_extract` (127), `_persist_tabular_rows` (106) and `_queue_embedding_backfill` (97). Split by stage behind a small `ProcessingWorker` facade; keep the public `run()`/`run_once()` contract for tests.

**Effort:** M–L · **Impact:** makes the next performance fix reviewable; no runtime change.

### P2-5. Add CI

There is no `.github/workflows` (or any CI configuration). `make check` is manual and does not run the Postgres/pgvector tests, the web checks, or the extraction tests. Add a workflow that runs:

- `uv sync --extra extraction`, `ruff check`, `ruff format --check`, `mypy`
- `pytest` (SQLite) and `pytest tests/test_postgres_integration.py` against a `pgvector/pgvector:pg17` service with `TEST_POSTGRES_URL`
- `cd web && npm ci && npm run check`
- optionally the `integration`-marked extraction tests on a worker image

**Effort:** S–M · **Impact:** prevents the known Postgres-only paths from regressing.

### P2-6. `make test`/`make check` fail on a fresh clone

**Where:** `pyproject.toml:38-45` (extraction extra), `Makefile:155,175`

`uv run pytest -q` fails during collection with `ModuleNotFoundError: openpyxl` because `tests/test_worker.py` imports the worker, which imports `app.extraction.xlsx_reader`; `openpyxl`, `pymupdf4llm` and `rapidocr` are only in the `extraction` extra. We had to run `uv sync --extra extraction` before the suite would collect. Either move the test-required parser packages into the `dev` group or make `test`/`check` run `uv run --extra extraction pytest`.

**Effort:** XS · **Impact:** the documented first-run path actually works.

### P2-7. Add metrics and structured logs

**Where:** `app/main.py`, `app/observability/tracing.py`, `worker/__main__.py`

Tracing is good, but there are no metrics: queue depth, run stage durations, search/embedding latency, embedding failures, tool error rates, token usage. The API uses uvicorn’s default logging; `request_id` is generated and returned in `X-Request-ID` but never logged, and the worker formatter ignores the `run_id` extra it receives. Add OTel/Prometheus metrics and a JSON log formatter with `request_id`/`trace_id`/`run_id`. Instrument the `RowQuery` engine (`app/retrieval/sql.py:323-328`) too, so model-authored SQL appears in traces.

**Effort:** M · **Impact:** makes the next incident diagnosable without a profiler.

### P2-8. Workbooks are not rejected by `read_document`/`outline`

**Where:** `app/retrieval/navigation.py:165,242` (checks only `CSV_MEDIA_TYPE`), `app/mcp/server.py:250-255`

XLSX is stored as rows just like CSV, but navigation only rejects CSV. `read_document`/`get_document_outline` on a workbook return “No chunks at or after ordinal 0” instead of pointing at `list_queryable_files`/`query_rows`. Use `ROW_STORED_MEDIA_TYPES` (as `AgentToolKit.describe_document` does) and update the MCP descriptions from “CSV” to “CSV or workbook”.

**Effort:** S · **Impact:** removes a misleading empty result.

### P2-9. `processing_runs` reports a capped page size as `total`

**Where:** `app/routes/processing_runs.py:63`

The query is capped at 200 rows, and the response returns `total=len(items)`. A client cannot tell a complete list from a truncated one. Add `has_more` (fetch 201, return 200) or a real count.

**Effort:** XS · **Impact:** accurate pagination semantics.

### P2-10. Documentation drift

- `app/README.md:119` says **XLSX is not accepted**, but the code supports it and the root README says so.
- `app/README.md:733` says the extraction sandbox has **no network**, but `tiktoken` performs an HTTPS download; the sandbox has resource limits and a scrubbed environment, not a network namespace.
- `README.md:76` says a **viewer can download**, but `ROLE_PERMISSIONS` and `tests/test_documents.py` deny `download` to viewers.
- `app/README.md:435` says CSV files are refused by navigation; workbooks are row-stored too.
- `app/README.md:116` says uploads default to **50 MiB**; `Settings.max_upload_bytes` and `.env.example` use **100 MiB**.

**Effort:** XS · **Impact:** operators and agents stop acting on false constraints.

### P2-11. Smaller cleanups

- `app/infrastructure/storage.py:267` uses `assert stream is not None`; replace with an explicit error so `python -O` cannot remove the check.
- `app/extraction/sandbox.py:23-34` passes `LD_PRELOAD` into the sandbox; remove it from the allowlist (keep `LD_LIBRARY_PATH` if the ONNX runtime needs it).
- `app/documents/conversion.py:50` runs LibreOffice in the worker thread, outside the resource-limited extraction sandbox; run it in a dedicated low-privilege container/sandbox or document the boundary.
- `app/main.py` exposes `/docs` and `/openapi.json` in every environment; gate them behind `environment != "production"` or an admin flag.
- Add `ruff format --check`, `pytest-cov` and a coverage threshold; clean up the 12 pytest warnings (sync tests marked `@pytest.mark.asyncio`).
- Add a `LICENSE` file if the repository is meant to be open source; `pyproject.toml` has no `license` field.
- The `AgentModelSetting` admin response returns `api_key_preview` and `key_hint` (both last four characters); one is enough.

---

## What not to do

- **Do not rewrite the API or worker in Go/Rust to fix latency.** The measured search path is 67% embedding model, 18% vector SQL and ~6% Python/protocol. Extraction is ~50% ONNX and ~12% PyMuPDF. A rewrite would keep the model and native costs and add FFI/packaging complexity.
- **Do not add in-container parser concurrency** expecting throughput. Two parsers measured 2.34 pages/s vs 2.45 pages/s serial, with +0.70 GB RSS on a 4-CPU container. Scale worker containers instead.
- **Do not tune the embedding batch size expecting a win.** On the same 16 texts, one batch of 16 took 2.121 s and 16 single requests took 2.140 s. Input length and model choice dominate.
- **Do not optimize the API routes first.** They are 3–5 ms; the search model and database query shape dominate.

## Suggested order

1. P0-1, P0-2, P0-5 (small, user-visible correctness).
2. P0-3, P0-4 (SQL boundary; add tests before exposing `query_rows` more widely).
3. P1-1 (largest measured latency win); P1-2 is withdrawn.
4. P1-6, P1-7, P1-8 (memory/DoS on user-controlled input).
5. P1-3, P1-4, P1-5 (index/query shape).
6. P1-9, P1-10, P1-11, P1-13 (deployment reliability).
7. P2-1, P2-2, P2-4, P2-5 (maintainability and CI).
