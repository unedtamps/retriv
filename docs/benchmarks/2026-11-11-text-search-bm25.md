# Text search: substring vs BM25 vs Postgres FTS vs ParadeDB

**Date:** 2026-11-11
**Harness:** `scripts/benchmark_text_search.py`
**Raw output:** [`2026-11-11-text-search-bm25.json`](2026-11-11-text-search-bm25.json)

The ParadeDB arm was run against a restored copy of the development database:
`paradedb/paradedb:17-v0.26.2` (PostgreSQL 17.11, `pg_search 0.26.2`,
`pgvector 0.8.6`) in a separate container on port 7433. The live database was
not modified.

## Question

`search_text` currently requires the whole query to appear as one contiguous,
case-insensitive substring (`text ILIKE '%query%'`). That is precise for
identifiers, but it fails when a long query contains the right terms in a
different order or with other words between them.

Would a token-based lexical ranking find those chunks, can it keep the
identifier precision the tool exists for, and does built-in Postgres FTS get
close enough to BM25 to avoid a new database extension?

## Setup

- **Corpus:** 3,111 active chunks in one workspace.
- **Queries:**
  - 4 real chat questions + 60 golden questions, used as long lexical strings;
  - 40 identifiers mined from the corpus (`INV-…`, `Pasal 7`, `Item 15`,
    `Form S-8`, and similar), each labelled with the chunks containing it;
  - 40 synthetic non-contiguous term queries built from four terms in a target
    chunk.
- **Methods:**
  - `ilike` — the production `ContextRetriever.search_text`;
  - `bm25` — Okapi BM25 (k1=1.5, b=0.75, stopwords removed) over the same chunks.
    The tokenizer keeps dotted, hyphenated, slashed and underscored strings
    (`10.10`, `INV-2024-001`, `UNDP/UNFPA/UNOPS`) as single terms;
  - `hybrid` — exact substring matches first, then BM25 for the rest;
  - `fts_en` / `fts_simple` — Postgres `to_tsvector` + `websearch_to_tsquery`
    with the query tokens joined by `or`, ranked by `ts_rank_cd`;
  - `hybrid_fts_*` — exact substring matches first, then that FTS ranking;
  - `paradedb` — ParadeDB `pg_search` `|||` match-disjunction, ranked by
    `pdb.score(id)`;
  - `hybrid_paradedb` — exact substring matches first, then ParadeDB.
- **Limit:** top 20 chunks.
- The FTS runs do **not** create an expression GIN index. That affects latency,
  not ranking quality.
- The restored copy contains the same 12 documents, 3,111 chunks and 3,111
  embeddings as the development database. The BM25 index is
  `document_chunks_bm25` on `(id, text)`.

## Results

### The 64 retrieval questions as long lexical queries

| Method | Chunk MRR | nDCG@10 | recall@12 | Section MRR | Zero-result |
|---|---:|---:|---:|---:|---:|
| `ILIKE` | 0.278 | 0.278 | 28% | 0.278 | **72%** |
| **BM25** | **0.699** | **0.752** | **94%** | **0.747** | **0%** |
| **BM25 hybrid** | **0.783** | **0.815** | **94%** | **0.809** | **0%** |
| FTS `english` | 0.186 | 0.245 | 47% | 0.261 | 0% |
| FTS `simple` | 0.196 | 0.254 | 50% | 0.280 | 0% |
| FTS `english` hybrid | 0.375 | 0.397 | 49% | 0.412 | 0% |
| FTS `simple` hybrid | 0.385 | 0.405 | 52% | 0.430 | 0% |
| **ParadeDB BM25** | **0.696** | 0.739 | 93% | 0.741 | 0% |
| **ParadeDB hybrid** | **0.785** | 0.807 | 93% | **0.809** | 0% |

The baseline returns nothing for 72% of the questions. BM25 finds a relevant
chunk for 94% within 12 results. Exact-first hybrid improves the first rank
over BM25 without changing recall.

**ParadeDB reproduces the BM25 result almost exactly** (0.696 vs 0.699 MRR;
hybrid 0.785 vs 0.783) while running inside Postgres. Built-in Postgres FTS is
**much worse than BM25** on the same queries: FTS hybrid MRR is 0.38 versus 0.78
for the BM25 hybrid. The gap is ranking quality, not just indexing.

### Identifier queries (the tool's original job)

| Method | Chunk MRR | recall@12 | Zero-result |
|---|---:|---:|---:|
| `ILIKE` | **1.000** | **100%** | 0% |
| BM25 | 0.699 | 98% | 0% |
| **BM25 hybrid** | **1.000** | **100%** | **0%** |
| FTS `english` | 0.317 | 92% | 0% |
| FTS `simple` | 0.319 | 92% | 0% |
| **FTS hybrid** | **1.000** | **100%** | **0%** |
| ParadeDB | 0.678 | **100%** | 0% |
| **ParadeDB hybrid** | **1.000** | **100%** | **0%** |

This is the reason not to replace substring search with pure BM25 or pure FTS.
The exact-first path is what keeps identifiers at rank 1.

### Synthetic non-contiguous term queries

| Method | Chunk MRR | nDCG@10 | recall@12 | Zero-result |
|---|---:|---:|---:|---:|
| `ILIKE` | 0.000 | 0.000 | 0% | **100%** |
| BM25 | **0.759** | **0.810** | **98%** | 0% |
| FTS `english` | 0.054 | 0.065 | 12% | 0% |
| FTS `simple` | 0.091 | 0.127 | 28% | 0% |
| ParadeDB | 0.757 | 0.789 | 95% | 0% |

This is a capability check, not a quality claim: the query is built from four
terms in the target chunk, so it is expected to favour BM25. The gap between
BM25 and FTS here is still striking.

### Real chat subset

| Method | Chunk MRR | nDCG@10 | recall@12 |
|---|---:|---:|---:|
| `ILIKE` | 0.000 | 0.000 | 0% |
| BM25 | 0.278 | 0.114 | 50% |
| FTS `english` | 0.064 | 0.035 | 50% |
| FTS `simple` | 0.108 | 0.042 | 75% |

Lexical search alone is weak on natural chat questions; `search_context` remains
the right tool for those.

### Latency

| Split | ILIKE p50 / p95 | BM25 p50 / p95 | BM25 hybrid | FTS `simple` | FTS hybrid | ParadeDB | ParadeDB hybrid |
|---|---:|---:|---:|---:|---:|---:|---:|
| All 64 | 26.6 / 30.0 ms | 0.7 / 1.4 ms | 27.7 / 30.4 ms | 369.9 / 547.8 ms | 396.3 / 573.7 ms | **4.3 / 6.8 ms** | 31.2 / 34.4 ms |
| Identifiers | 28.4 / 30.0 ms | 0.4 / 0.6 ms | 28.9 / 30.5 ms | 277.6 / 284.5 ms | 307.0 / 315.3 ms | **2.2 / 3.0 ms** | 30.7 / 32.6 ms |

The BM25 number is **in-memory scoring over a prebuilt index**; it excludes
database I/O and index construction. The FTS number has **no expression index**;
a GIN index would make it much faster, but not rank better. ParadeDB is using a
real BM25 index and includes the database round-trip. The `ILIKE` number also
includes the database round-trip. The hybrid numbers add the exact-substring
query on top of the lexical ranking.

## Interpretation

- **BM25 solves the long-query gap.** A query whose terms are spread across a
  chunk now finds it; the contiguous-substring tool does not.
- **Pure BM25 is not safe for identifiers.** It drops identifier MRR from 1.0 to
  0.70. Exact-first hybrid keeps it at 1.0.
- **Built-in Postgres FTS is not a substitute for BM25.** `ts_rank_cd` has no
  IDF weighting; with OR-joined terms, common terms dominate the ranking. The
  exact-first layer recovers identifiers but long-query MRR stays around 0.38,
  less than half of the BM25 hybrid.
- **ParadeDB reproduces the BM25 result inside Postgres.** 0.696 versus 0.699
  MRR, and 0.785 versus 0.783 for the exact-first hybrid. It also runs in
  **4.3 ms p50 / 6.8 ms p95** on the 3,111-chunk corpus, versus 370 ms for
  unindexed FTS. This is the first production-shaped option that keeps the gain.
- **Do not add a new tool.** Keep one lexical tool (`search_text`) and change its
  internals. A second lexical tool would create tool-selection ambiguity for the
  agent.

## Caveats

- The golden questions were generated from chunks, so they naturally share terms
  with the target. The 64-question row measures lexical capability, not overall
  retrieval quality, and is not a substitute for `search_context`.
- Only four real chat questions are available; they are too few to judge chat
  behaviour.
- The synthetic term set is constructed from the target chunk and is deliberately
  favourable to BM25.
- The BM25 implementation is Python and in-memory; it is a prototype, not a
  production path.
- The current Postgres image has `pg_trgm` and `unaccent` available but **no
  `pg_search`**, so true BM25 in production would require ParadeDB or an
  external engine.
- The ParadeDB arm ran on a **restored copy**, not the live database. The copy
  used `pgvector 0.8.6` while the live database has `0.8.7`; the dump restored
  cleanly, but verify that version path before swapping the live volume.
- The ParadeDB index is only on `(id, text)`. Production may want to tune the
  tokenizer, include section/filename fields, or set a language-specific
  stemmer.
- The benchmark covers one workspace. Index build time and concurrent query
  behavior are not measured.
- `search_text` candidate scores are currently all `1.0`, so the BGE blend
  degenerates to pure BGE on that path. That interaction is not covered here.

## Reproducing the ParadeDB arm

The test instance was isolated on port 7433; the development database on 7432
was untouched.

```bash
# 1. Start ParadeDB with a fresh volume
docker run -d --name retriv-paradedb-test \
  -e POSTGRES_USER="$POSTGRES_USER" \
  -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
  -e POSTGRES_DB=postgres \
  -p 127.0.0.1:7433:5432 \
  -v retriv-paradedb-test-data:/var/lib/postgresql/data \
  paradedb/paradedb:17-v0.26.2

# 2. Create the target database and restore the dump
docker exec retriv-paradedb-test createdb -U "$POSTGRES_USER" retriv_paradedb
docker exec retriv-api-postgres-1 pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc \
  | docker exec -i retriv-paradedb-test pg_restore \
      -U "$POSTGRES_USER" -d retriv_paradedb --no-owner --no-privileges

# 3. Enable the extension and build the BM25 index
docker exec retriv-paradedb-test psql -U "$POSTGRES_USER" -d retriv_paradedb \
  -c "CREATE EXTENSION pg_search CASCADE;" \
  -c "CREATE INDEX document_chunks_bm25 ON document_chunks USING bm25 (id, text);"

# 4. Run the harness against the copy
RETRIV_DATABASE_URL="$PARADEDB_TEST_URL" \
  uv run python scripts/benchmark_text_search.py --paradedb \
  --out docs/benchmarks/2026-11-11-text-search-bm25.json
```

## Recommendation

1. **Do not ship built-in Postgres FTS** as the lexical upgrade. It is far behind
   BM25 on long queries and only reaches identifier parity through the
   exact-first layer.
2. **ParadeDB is the viable production path.** It reproduced the BM25 quality
   (0.696 vs 0.699 MRR; 0.785 vs 0.783 hybrid) at 4.3 ms p50. It is a Postgres
   extension, so the deployment is a database image swap or an extension
   install, not an extra search container.
3. Before swapping the live database: take a backup, restore into a fresh
   ParadeDB volume, verify `pgvector` compatibility, run the app migrations, and
   recreate the BM25 index. The test container on port 7433 already proves the
   dump restores cleanly.
4. Keep the **exact-first hybrid contract** regardless of backend: exact
   substring matches rank first, lexical ranking fills the rest. This is what
   keeps identifier MRR at 1.0.
5. Update the tool description and system prompt to say “finds chunks containing
   the query’s terms, not necessarily as one phrase” so the agent actually uses
   the new behaviour.
6. If the image swap is not justified, keep `ILIKE` and accept the long-query
   gap; built-in FTS is not a good middle ground here.

## Live switch

The development compose stack now uses
`paradedb/paradedb:17-v0.26.2` with a fresh volume
(`retriv-api_postgres_data_paradedb`). The pre-ParadeDB volume
(`retriv-api_postgres_data`) is retained for rollback. The BM25 index is
created by migration `0006_bm25_index`.

## Implemented behaviour

`search_text` now runs the exact substring query first. If it finds a match,
those rows are returned unchanged and BM25 is not consulted; identifiers keep
their exact-first behaviour. If the exact pass is empty, the retriever checks
for `pg_search` and the `document_chunks_bm25` index and, when both exist,
runs the ParadeDB `|||` / `pdb.score(id)` query. A database without the
extension or index keeps the old exact-only behaviour.

The benchmark's `hybrid_paradedb` arm fills the remaining slots with BM25 after
exact matches; the implementation returns exact matches only when they exist.
The measured MRR and recall are identical on all five query sets because every
query with an exact hit already had enough exact rows to fill the metric window.
