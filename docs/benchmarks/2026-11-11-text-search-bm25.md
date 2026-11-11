# Text search: substring vs BM25 vs hybrid

**Date:** 2026-11-11
**Harness:** `scripts/benchmark_text_search.py`
**Raw output:** [`2026-11-11-text-search-bm25.json`](2026-11-11-text-search-bm25.json)

## Question

`search_text` currently requires the whole query to appear as one contiguous,
case-insensitive substring (`text ILIKE '%query%'`). That is precise for
identifiers, but it fails when a long query contains the right terms in a
different order or with other words between them.

Would a token-based lexical ranking find those chunks, and can it do so without
losing the identifier precision the tool exists for?

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
  - `hybrid` — exact substring matches first, then BM25 for the rest.
- **Limit:** top 20 chunks.

## Results

### The 64 retrieval questions as long lexical queries

| Method | Chunk MRR | nDCG@10 | recall@12 | Section MRR | Zero-result |
|---|---:|---:|---:|---:|---:|
| `ILIKE` | 0.278 | 0.278 | 28% | 0.278 | **72%** |
| BM25 | 0.699 | 0.752 | 94% | 0.747 | **0%** |
| **Hybrid** | **0.783** | **0.815** | **94%** | **0.809** | **0%** |

The baseline returns nothing for 72% of the questions. BM25 finds a relevant
chunk for 94% within 12 results. Exact-first hybrid improves the first rank
over BM25 without changing recall.

### Identifier queries (the tool's original job)

| Method | Chunk MRR | recall@12 | Zero-result |
|---|---:|---:|---:|
| `ILIKE` | **1.000** | **100%** | 0% |
| BM25 | 0.699 | 98% | 0% |
| **Hybrid** | **1.000** | **100%** | **0%** |

This is the reason not to replace substring search with pure BM25. BM25 ranks
the exact identifier at position 2–13 for many queries because term frequency
and document length can outweigh the exact string (`Exhibit 10.10`,
`FORWARD-LOOKING`). Exact-first hybrid keeps the identifier at rank 1. Postgres
FTS has the same class of problem: its parser splits `INV-2024-001` into
`inv`, `-2024`, `-001`, so an exact-string fast path is still needed.

### Synthetic non-contiguous term queries

| Method | Chunk MRR | nDCG@10 | recall@12 | Zero-result |
|---|---:|---:|---:|---:|
| `ILIKE` | 0.000 | 0.000 | 0% | **100%** |
| BM25 | 0.759 | 0.810 | 98% | 0% |
| Hybrid | 0.759 | 0.810 | 98% | 0% |

This is a capability check, not a quality claim: the query is built from four
terms in the target chunk, so it is expected to favour BM25.

### Real chat subset

| Method | Chunk MRR | nDCG@10 | recall@12 |
|---|---:|---:|---:|
| `ILIKE` | 0.000 | 0.000 | 0% |
| BM25 | 0.278 | 0.114 | 50% |

Lexical search alone is weak on natural chat questions; `search_context` remains
the right tool for those. The win is for identifier and keyword queries, not for
replacing semantic retrieval.

### Latency

| Split | ILIKE p50 / p95 | BM25 p50 / p95 | Hybrid p50 / p95 |
|---|---:|---:|---:|
| All 64 | 26.7 / 30.7 ms | 0.6 / 1.1 ms | 27.5 / 31.1 ms |
| Identifiers | 29.1 / 30.8 ms | 0.3 / 0.4 ms | 29.4 / 31.1 ms |

The BM25 number is **in-memory scoring over a prebuilt index**; it excludes
database I/O and index construction. The `ILIKE` number includes the database
round-trip. A production Postgres FTS query will be slower than 0.6 ms, though
still expected to be millisecond-scale.

## Interpretation

- **BM25 solves the long-query gap.** A query whose terms are spread across a
  chunk now finds it; the contiguous-substring tool does not.
- **Pure BM25 is not safe for identifiers.** It drops identifier MRR from 1.0 to
  0.70.
- **Exact-first hybrid keeps both.** Identifier MRR stays 1.0, long-query MRR
  rises from 0.278 to 0.783.
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
- This benchmark uses a Python BM25 implementation. The current Postgres image
  has `pg_trgm` and `unaccent` available but **no `pg_search`**, so true BM25 in
  production would require ParadeDB or an external engine.
- `search_text` candidate scores are currently all `1.0`, so the BGE blend
  degenerates to pure BGE on that path. That interaction is not covered here.

## Recommendation

1. Benchmark **Postgres FTS + exact-first hybrid** (`websearch_to_tsquery` or an
   OR-term `to_tsquery` plus `ts_rank_cd`) on the same sets. If it captures most
   of the BM25 gain, implement it without new infrastructure.
2. If FTS ranking is clearly insufficient, evaluate ParadeDB `pg_search` for true
   BM25.
3. Keep `search_context` for concepts and paraphrases; use the upgraded
   `search_text` for identifiers and keyword strings.
4. Update the tool description and system prompt to say “finds chunks containing
   the query’s terms, not necessarily as one phrase” so the agent actually uses
   the new behaviour.
