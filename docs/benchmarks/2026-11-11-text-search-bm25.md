# Text search: substring vs BM25 vs Postgres FTS

**Date:** 2026-11-11
**Harness:** `scripts/benchmark_text_search.py`
**Raw output:** [`2026-11-11-text-search-bm25.json`](2026-11-11-text-search-bm25.json)

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
  - `hybrid_fts_*` — exact substring matches first, then that FTS ranking.
- **Limit:** top 20 chunks.
- The FTS runs do **not** create an expression GIN index. That affects latency,
  not ranking quality.

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

The baseline returns nothing for 72% of the questions. BM25 finds a relevant
chunk for 94% within 12 results. Exact-first hybrid improves the first rank
over BM25 without changing recall.

Built-in Postgres FTS is **much worse than BM25** on the same queries. Adding an
exact-first layer fixes identifiers, but does not close the ranking gap: FTS
hybrid MRR is 0.38 versus 0.78 for BM25 hybrid.

### Identifier queries (the tool's original job)

| Method | Chunk MRR | recall@12 | Zero-result |
|---|---:|---:|---:|
| `ILIKE` | **1.000** | **100%** | 0% |
| BM25 | 0.699 | 98% | 0% |
| **BM25 hybrid** | **1.000** | **100%** | **0%** |
| FTS `english` | 0.317 | 92% | 0% |
| FTS `simple` | 0.319 | 92% | 0% |
| **FTS hybrid** | **1.000** | **100%** | **0%** |

This is the reason not to replace substring search with pure BM25 or pure FTS.
The exact-first path is what keeps identifiers at rank 1.

### Synthetic non-contiguous term queries

| Method | Chunk MRR | nDCG@10 | recall@12 | Zero-result |
|---|---:|---:|---:|---:|
| `ILIKE` | 0.000 | 0.000 | 0% | **100%** |
| BM25 | **0.759** | **0.810** | **98%** | 0% |
| FTS `english` | 0.054 | 0.065 | 12% | 0% |
| FTS `simple` | 0.091 | 0.127 | 28% | 0% |

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

| Split | ILIKE p50 / p95 | BM25 p50 / p95 | BM25 hybrid | FTS `simple` | FTS hybrid |
|---|---:|---:|---:|---:|---:|
| All 64 | 26.3 / 29.4 ms | 0.7 / 1.1 ms | 27.1 / 29.8 ms | 359.5 / 511.3 ms | 386.4 / 536.4 ms |
| Identifiers | 28.6 / 29.5 ms | 0.4 / 0.5 ms | 29.0 / 29.8 ms | 270.2 / 279.9 ms | 298.6 / 308.8 ms |

The BM25 number is **in-memory scoring over a prebuilt index**; it excludes
database I/O and index construction. The FTS number has **no expression index**;
a GIN index would make it much faster, but not rank better. The `ILIKE` number
includes the database round-trip.

## Interpretation

- **BM25 solves the long-query gap.** A query whose terms are spread across a
  chunk now finds it; the contiguous-substring tool does not.
- **Pure BM25 is not safe for identifiers.** It drops identifier MRR from 1.0 to
  0.70. Exact-first hybrid keeps it at 1.0.
- **Built-in Postgres FTS is not a substitute for BM25.** `ts_rank_cd` has no
  IDF weighting; with OR-joined terms, common terms dominate the ranking. The
  exact-first layer recovers identifiers but long-query MRR stays around 0.38,
  less than half of the BM25 hybrid.
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
- `search_text` candidate scores are currently all `1.0`, so the BGE blend
  degenerates to pure BGE on that path. That interaction is not covered here.

## Recommendation

1. **Do not ship built-in Postgres FTS** as the lexical upgrade. It is far behind
   BM25 on long queries and only reaches identifier parity through the
   exact-first layer.
2. If the BM25 gain is worth the infrastructure change, benchmark **ParadeDB
   `pg_search`** on a copy of the database. The ParadeDB PostgreSQL image
   includes both `pg_search` and `pgvector`, so this is a database image swap,
   not an extra search container. Verify the pgvector version first.
3. Keep the **exact-first hybrid contract** regardless of backend: exact
   substring matches rank first, lexical ranking fills the rest.
4. Update the tool description and system prompt to say “finds chunks containing
   the query’s terms, not necessarily as one phrase” so the agent actually uses
   the new behaviour.
5. If the image swap is not justified, keep `ILIKE` and accept the long-query
   gap; built-in FTS is not a good middle ground here.
