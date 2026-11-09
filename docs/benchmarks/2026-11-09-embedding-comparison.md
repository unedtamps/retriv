# Embedding model comparison — Qwen3-Embedding-8B vs nomic-embed-text-v1.5

**Date:** 2026-11-09  
**Corpus:** 9 English financial documents, 2,691 chunks, one workspace  
**Queries:** 60 fresh English questions generated from random chunks of the current corpus  
**Runtime:** `llama.cpp` 2.16.0, RTX 4060 Laptop 8 GB, one model loaded at a time, no LM Studio  
**Models:** `Qwen3-Embedding-8B-Q4_K_M` (4,096 dims, last-token pooling) and `nomic-embed-text-v1.5-Q4_K_M` (768 dims, mean pooling, `search_document:`/`search_query:` prefixes)

Raw data: [`2026-11-09/embedding-model-comparison.json`](2026-11-09/embedding-model-comparison.json)  
Golden set: [`2026-11-09/golden-current.json`](2026-11-09/golden-current.json)

---

## TL;DR

- **Do not replace Qwen3-8B with nomic on this corpus if retrieval quality matters.** Nomic is 24–36× faster and 56× smaller, but it loses **20 percentage points of recall@8** (95% → 75%) and **0.17 MRR** (0.734 → 0.561).
- **A free storage win exists with Qwen3:** truncating Qwen3’s vectors from 2,000 to 768 dimensions (Matryoshka) cost only **1.7 pp recall@8** (95% → 93.3%) and **0.01 MRR**, statistically indistinguishable on this set. That cuts vector/index size ~2.6×.
- Nomic’s required prefixes matter: without them it scored 70% recall@8; with `search_query:`/`search_document:` it scored 75%. Even best-practice nomic remains far behind Qwen3 here.
- Batch size is neutral (confirmed separately); the model is the lever.

---

## 1. Method

### Corpus and queries

The existing `evals/retrieval_golden.json` is stale — its workspace and chunk IDs are not in the current database. I generated a fresh set:

- 60 chunks sampled from the current 2,691 indexed chunks (`token_count >= 100`, seed `0.42`).
- `qwen3.5-4b` wrote one specific question from each passage, with thinking disabled.
- The source chunk is the expected answer.
- The questions are English and drawn from the financial/legal corpus (Anthropic prospectus, 3M/Activision 10-Ks, UNDP regulations, etc.).
- No manual relabeling was performed; the expected chunk is the passage the question was written from.

### Vector spaces

- **Qwen3 chunk vectors:** the production vectors already stored in PostgreSQL (2,000 dims, truncated from 4,096 by the pipeline).
- **Qwen3 query vectors:** generated through `llama.cpp`; verified against the stored chunk vectors — a chunk re-embedded through `llama.cpp` matched its stored vector at **cosine 1.0**.
- **Qwen3 1,024 / 768 dims:** Matryoshka truncation of the same vectors, re-normalized. This isolates the dimension effect from the model effect.
- **Nomic chunk/query vectors:** generated fresh through `llama.cpp` with the model’s required `search_document: ` / `search_query: ` prefixes.
- **Nomic without prefixes:** kept as a control, to show the prefix effect.

### Metrics

- Cosine ranking over all 2,691 chunks in the workspace.
- **recall@1, recall@5, recall@8, recall@20** — whether the expected chunk appears by rank *k*.
- **MRR@8** — mean reciprocal rank within the top 8.
- **section-completed recall@12** — a simulation of the production `search_context` behaviour: top-8 raw matches, then fill each matched section up to 4 chunks, total budget 12.
- Paired bootstrap over the 60 queries (10,000 resamples) for 95% confidence intervals.

### Caveats

- **60 questions, one corpus.** The intervals are wide; treat the direction as solid and the exact gap as approximate.
- Questions were generated from the source chunk, so a different chunk could also answer some of them. Recall is therefore a lower bound, but the same bias applies to both models.
- The corpus is **English financial/legal text**. Nomic’s English weakness here may be larger in the project’s Indonesian/legal material, which was not evaluated.
- Dimension-truncation results were measured in memory; production adoption needs a schema migration and reindex.
- The Qwen3 instruction prefix (`Instruct: ... Query: ...`) was not tested; production currently embeds the raw query.

---

## 2. Retrieval quality

| Model | recall@1 | recall@5 | recall@8 | recall@20 | MRR@8 | section-completed recall@12 |
|---|---:|---:|---:|---:|---:|---:|
| **Qwen3-8B, 2,000 dims (production)** | **60.0%** | **90.0%** | **95.0%** | **98.3%** | **0.734** | **95.0%** |
| Qwen3-8B, 1,024 dims | 61.7% | 91.7% | 93.3% | 96.7% | 0.733 | 93.3% |
| Qwen3-8B, 768 dims | 60.0% | 86.7% | 93.3% | 96.7% | 0.724 | 93.3% |
| nomic-1.5, 768 dims, with prefixes | 46.7% | 60.0% | 75.0% | 81.7% | 0.561 | 76.7% |
| nomic-1.5, 768 dims, no prefixes | 38.3% | 60.0% | 70.0% | 81.7% | 0.467 | 73.3% |

### Paired differences (95% bootstrap CI)

**nomic with prefixes vs Qwen3-2,000:**

| Metric | Qwen3 | Nomic | Difference | 95% CI |
|---|---:|---:|---:|---:|
| recall@1 | 60.0% | 46.7% | **−13.3 pp** | [−26.7, 0.0] |
| recall@8 | 95.0% | 75.0% | **−20.0 pp** | [−30.0, −10.0] |
| MRR@8 | 0.734 | 0.561 | **−0.173** | [−0.277, −0.073] |

The recall@8 and MRR losses are statistically clear. The recall@1 interval touches zero, so the top-1 gap is directionally negative but not significant at this sample size.

**Qwen3-768 vs Qwen3-2,000:**

| Metric | Qwen3-2,000 | Qwen3-768 | Difference | 95% CI |
|---|---:|---:|---:|---:|
| recall@1 | 60.0% | 60.0% | 0.0 pp | [−8.3, +8.3] |
| recall@8 | 95.0% | 93.3% | −1.7 pp | [−5.0, 0.0] |
| MRR@8 | 0.734 | 0.724 | −0.010 | [−0.053, +0.033] |

The 768-dimension Qwen3 model is **statistically indistinguishable** from the 2,000-dimension version on this set, while using ~2.6× less vector storage and index memory.

---

## 3. Speed and memory

Measured on the same `llama.cpp` GPU runtime, one model loaded at a time:

| Workload | Qwen3-8B | nomic-1.5 | Speed-up |
|---|---:|---:|---:|
| 16 medium chunks, one batch | 2,042.9 ms | **56.2 ms** | **36.4×** |
| per chunk in that batch | 127.7 ms | **3.5 ms** | 36.4× |
| 60 golden queries | 2,203.5 ms | **90.2 ms** | **24.4×** |
| per query | 36.7 ms | **1.5 ms** | 24.4× |
| full 2,691-chunk corpus | ~6–11 min (length-dependent) | **17.9 s (6.6 ms/chunk)** | ~20–36× |

CPU fallback for nomic (no GPU) was much slower: **462.6 s for 2,691 chunks, 171.9 ms/chunk**. Nomic’s speed win assumes GPU inference.

### Storage / VRAM

| | Qwen3-8B | nomic-1.5 |
|---|---:|---:|
| Model file | 4.68 GB | **84 MB** |
| Stored vector, 2,000 dims | 8 KB | — |
| Stored vector, 768 dims | 3 KB | 3 KB |
| HNSW index, 2,691 chunks | 40 MB | ~15 MB (est.) |
| HNSW index, 1M chunks | ~15 GB | ~5.7 GB (est.) |
| GPU footprint in this test | ~5.3 GB | **~0.4 GB** |

Nomic frees roughly **6 GB of VRAM** and shrinks the vector index ~2.6×. Qwen3 at 768 dims gets the same storage/index saving without changing the model or the 8B query cost.

---

## 4. Interpretation

### Nomic is the wrong trade for this corpus

The speed numbers are excellent — 24× on query embedding and 36× on ingestion, with a 56× smaller model and ~6 GB of VRAM freed. But the retrieval cost is large:

- **1 in 4 queries misses the correct chunk in the top 8** (25% vs 5%).
- **MRR falls from 0.734 to 0.561**, meaning the correct chunk is materially further down the list even when it is found.
- Section completion recovers only ~1.7 pp, so the final context is still worse.
- Without nomic’s required prefixes, quality drops further (70% recall@8), so any nomic integration must add `search_document:`/`search_query:` in both the worker and the query path.

For a document-QA product where the model answers only from retrieved evidence, a 20 pp recall loss is likely to show up as more “the document does not say” answers and more wrong citations. That is not a safe default.

### Qwen3 at 768 dims is the actual free lunch

Qwen3’s Matryoshka embedding can be truncated to 768 dims with no measurable quality loss on this set:

- recall@8: 95.0% → 93.3% (−1.7 pp, CI [−5, 0])
- MRR@8: 0.734 → 0.724 (−0.01, CI [−0.05, +0.03])
- storage/index: ~2.6× smaller

The query cost stays the same (8B forward pass), so this is a **storage/memory** optimisation, not a latency one. It needs a migration from `vector(2000)` to `vector(768)` (or a half-vector equivalent), a reindex, and a second evaluation on a larger/human-checked set before changing production.

### If latency is the priority

If 36 ms per query is unacceptable and a 5% “top-1” / 20 pp “top-8” recall loss is acceptable, nomic is a coherent choice for a latency-first deployment — but it should be presented as a **different, lower-recall retrieval mode**, not a silent replacement. A middle option is a smaller Qwen3 embedding model rather than nomic; that was not evaluated here.

---

## 5. Recommendation

1. **Keep Qwen3-Embedding-8B for retrieval quality.** Nomic’s 20 pp recall@8 loss is too large for a citation-grounded document assistant on this English corpus.
2. **Evaluate Qwen3 at 768 dims for storage.** The measured quality loss is within noise; the index/vector reduction is ~2.6×. This requires a schema migration and reindex.
3. **Do not tune batch size.** The controlled same-text test showed 2.121 s as one batch vs 2.140 s as 16 single requests; grouping is neutral.
4. **If nomic is revisited**, add its required prefixes, test on the actual multilingual/Indonesian corpus, and treat the recall drop as a product decision. The current evaluation is English-only by request.

---

## 6. Reproduce

- Golden set generation: `/tmp/opencode/generate_golden.py` (60 questions, qwen3.5-4b, thinking disabled).
- Qwen3 query vectors: `/tmp/opencode/embed_queries.py` against a `llama.cpp` embedding server.
- Nomic vectors: `/tmp/opencode/embed_nomic_prefixed.py` with `search_document:`/`search_query:`.
- Scoring: `/tmp/opencode/score_models.py`; raw output in `docs/benchmarks/2026-11-09/embedding-model-comparison.json`.

The temporary `llama.cpp` servers were stopped after the run; LM Studio remains disabled, and the retriv worker is stopped because it has no embedding endpoint configured. Start the worker again once an embedding endpoint is restored.
