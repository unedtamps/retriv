# Optional reranking: Laya, Jev free, and BGE

**Status:** implemented, **disabled by default**. Laya and Jev free are not recommended. The local
`bge-reranker-v2-m3` cross-encoder, **blended with cosine at weight 0.3**, is the first configuration
to pass the ranking gate; it stays opt-in because it adds roughly 370 ms p50 / 500 ms p95 on the
test machine.

This folder contains the reproducible benchmarks, the fine-tuning dataset recipe, and the
comparisons behind the current default.

## What was built

`search_context` and `search_text` can pass their candidate pool through an optional reranker
before the agent sees the results.

- **Admin-controlled, global, off by default.** Configure it in **Platform administration →
  Agent harness → Retrieval → Reranking**.
- **Provider:** `laya` (pointwise decision model on the embedding endpoint) or `bge` (local
  llama.cpp cross-encoder at `RETRIV_RERANK_BASE_URL`).
- **Candidate pool:** 30 cosine candidates by default.
- **Final context:** the existing cap (`max_context_sources = 12`, section completion included).
- **Laya scoring:** each candidate is sent pointwise to `/v1/systemone` as a `score` question with
  the rubric `irrelevant / partially relevant / directly answers`; 8 requests concurrently.
- **BGE scoring:** the whole pool goes to `/v1/rerank` in one request. The raw relevance score is
  min-max normalised per query and blended with the normalised cosine score; 0.3 measured best,
  while 1.0 is pure reranking.
- **Fail open:** a timeout, non-200 response, or malformed answer returns the original cosine
  order and logs a warning. The agent never loses results because the reranker is down.
- **Scope:** only the two search tools. `read_document` and `query_rows` keep their existing
  behaviour.

One caveat: `search_text` candidates carry a uniform text-match score of 1.0, so the blend
degenerates to pure BGE ordering on that path. The ranking benchmark below covers vector search;
text search keeps the same admin switch but was not separately benchmarked.

The implementation lives in:

- `app/retrieval/rerank.py` — Laya pointwise and BGE bulk/blend adapters, fallback.
- `app/retrieval/context.py` — candidate over-fetch, provider dispatch, rerank for both search paths.
- `app/core/config.py` — `RETRIV_RERANK_BASE_URL`.
- `app/routes/admin.py` — `GET/PUT /v1/admin/retrieval` and `POST /v1/admin/retrieval/test`.
- `web/src/components/AdminRerank.tsx` — the admin card.
- `alembic/versions/0004_retrieval_settings.py` — the singleton settings table.
- `alembic/versions/0005_rerank_provider.py` — provider and blend columns.
- `scripts/benchmark_bge_reranker.py` — the BGE benchmark.

## Reranker servers

### Laya (LM Studio)

Laya must be loaded with a physical batch large enough for the passage batch. On the test machine
the default `physical_batch_size = 512` produced HTTP 500s for multi-candidate requests; loading it
with `eval_batch_size = 4096` and `physical_batch_size = 4096` fixed that:

```bash
curl -s http://127.0.0.1:1234/api/v1/models/load \
  -H "Authorization: Bearer $LM_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"laya","context_length":8192,"eval_batch_size":4096,"physical_batch_size":4096}'
```

The adapter uses the same base URL and key as the embedding model, because one LM Studio instance
serves both Qwen embeddings and Laya decisions.

### BGE (llama.cpp)

`bge-reranker-v2-m3` is served by llama.cpp, not LM Studio. The physical batch must cover the
candidate pool: the default 512 failed with HTTP 500 on a 30-document request. The measured command
is:

```bash
llama-server \
  -m ~/.lmstudio/models/gpustack/bge-reranker-v2-m3-GGUF/bge-reranker-v2-m3-Q8_0.gguf \
  --reranking -b 4096 -ub 4096 --host 127.0.0.1 --port 8099 --no-webui
```

It exposes `POST /v1/rerank` with `{model, query, documents[]}` and returns one
`relevance_score` per document. Set `RETRIV_RERANK_BASE_URL` to that server; no API key is used
for the local endpoint.

## Laya benchmark

Method:

- **64 questions:** 4 real questions from the local chat archive, plus the 60-question
  `evals/retrieval_golden.json` set.
- Real-chat relevance labels are the chunks the assistant actually cited (document + ordinal →
  active chunk ID). Golden labels are the chunk each question was generated from.
- Each question retrieves **30 cosine candidates**, then Laya scores those candidates pointwise.
- Metrics: MRR, nDCG@10, recall@12, p50 latency.
- The full run is `scripts/benchmark_rerank.py`; raw output is
  [`benchmark-2026-11-11.json`](benchmark-2026-11-11.json).

### Zero-shot Laya vs cosine (64 questions)

| Set | Metric | Cosine | Laya zero-shot |
|---|---|---:|---:|
| All | MRR | **0.706** | 0.205 |
| All | nDCG@10 | **0.752** | 0.293 |
| All | recall@12 | **95%** | 67% |
| Real chat (4) | MRR | **0.500** | 0.150 |
| Real chat (4) | nDCG@10 | **0.333** | 0.203 |
| Golden (60) | MRR | **0.720** | 0.209 |
| Golden (60) | nDCG@10 | **0.780** | 0.299 |

Laya answered a 30-candidate search in a median **678 ms** and consumed about **10k input tokens**
per query. It is local, so that is GPU time rather than metered API spend, but it is still a
noticeable latency addition.

### Held-out comparison (16 questions: 4 real chat + 12 golden)

| Ranker | MRR | nDCG@10 | recall@12 |
|---|---:|---:|---:|
| **Cosine** | **0.516** | **0.559** | **93.8%** |
| Laya base, score head | 0.466 | 0.452 | 75.0% |
| Laya base, noul head | 0.446 | 0.456 | 68.8% |
| Fine-tuned score head | 0.113 | 0.057 | 18.8% |
| Fine-tuned noul head | 0.448 | 0.457 | 68.8% |

## Fine-tuning experiment

Two head-only fine-tunes were trained from `convaiinnovations/laya` with the encoder frozen:

- **Data:** 287 query-passage cases from 48 training queries; 5 hard cosine negatives per query.
  Evaluation: 103 cases from 16 held-out queries (the four real chat questions and 12 golden
  questions).
- **Recipe:** 4 epochs, soft cross-entropy, micro-batch 1, gradient accumulation 8, 132 optimizer
  updates, calibration on the trainer's held-out slice.
- **Score head:** passage accuracy 0.1845 → **0.5631**, ECE 0.389 → 0.194.
- **Noul head:** accuracy 0.6796 → **0.7864**, ECE 0.105 → 0.141.

Passage classification improved, but **retrieval ranking did not**: the fine-tuned score head
collapsed to MRR 0.113, and the fine-tuned noul head (MRR 0.448) stayed below cosine (0.516). The
base noul head scored 0.446, so the fine-tune made no meaningful difference at the ranking level.

The checkpoints were produced locally under `checkpoints/` and are not committed. The training
data is regenerated by `scripts/build_rerank_training.py`; its JSONL files are not committed
because they contain document passages.

### Why it lost

- Laya's own model card says the base checkpoints are near chance zero-shot on typed decisions;
  the published 0.766 number belongs to a checkpoint fine-tuned on the benchmark's own training
  split.
- The real-chat set is only four questions, and hard negative labels are noisy: a top cosine
  candidate that the assistant did not cite is not necessarily irrelevant.
- A head-only fine-tune on 287 cases is a small signal; the encoder was frozen.
- Qwen3-Embedding-8B cosine is already strong on this corpus: 95% recall@12, so a reranker mostly
  has to reorder a list that already contains the answer. A small decision head did not learn that
  ordering.

## BGE cross-encoder benchmark

Method:

- **Same 64 questions** as the Laya run: 4 real chat questions plus the 60-question golden set.
- **Three relevance levels**, because a single-chunk label is too harsh for a reranker:
  - **chunk** — the exact cited/generated chunk;
  - **section** — any chunk in the same document whose section path matches the label's section
    (a prefix counts, so a parent section is answerable);
  - **document** — any chunk in the same document.
- Each question retrieves **30 cosine candidates**; BGE scores the same pool in one `/v1/rerank`
  request with 1,200 characters per passage.
- Harness: `scripts/benchmark_bge_reranker.py`; raw output:
  [`bge-reranker-2026-11-11.json`](bge-reranker-2026-11-11.json). The 16-question held-out subset
  is in [`bge-reranker-heldout-2026-11-11.json`](bge-reranker-heldout-2026-11-11.json).

### Results (64 questions)

| Level | Ranker | MRR | nDCG@10 | recall@12 |
|---|---|---:|---:|---:|
| Chunk | Cosine | 0.707 | 0.758 | 95.3% |
| Chunk | BGE, pure rerank | 0.629 | 0.655 | 79.7% |
| Chunk | **BGE 0.3 blend** | **0.762** | **0.788** | 93.8% |
| Section | Cosine | 0.728 | 0.696 | 95.3% |
| Section | BGE, pure rerank | 0.723 | 0.656 | 85.9% |
| Section | **BGE 0.3 blend** | **0.787** | **0.733** | **95.3%** |
| Document | Cosine | 0.894 | 0.742 | 100% |
| Document | **BGE, pure rerank** | **0.940** | **0.775** | 100% |
| Document | BGE 0.3 blend | 0.922 | 0.764 | 100% |

**Pure BGE is not shippable as a replacement.** It improves the first relevant rank, especially at
document level, but it demotes answers out of the retrieval window: chunk recall falls from 95% to
80%, and section recall from 95% to 86%. A better first hit is not worth losing a source entirely.

**The 0.3 blend passes the gate.** At section level it improves MRR by **+0.059** and nDCG@10 by
**+0.037** with **no recall loss**. At chunk level it improves MRR by **+0.055** and nDCG@10 by
**+0.030**, giving up **one** query's recall (Activision net-income, rank 9 → 22); that chunk's
section still survives in the top 12. At document level it improves MRR by +0.028 and nDCG@10 by
+0.022. The gains also hold on the golden-only subset (chunk MRR 0.721 → 0.771, section 0.743 →
0.798) and on the four real chat questions (chunk/section MRR 0.500 → 0.625).

Per-query at section level: **12 wins, 4 losses, 48 ties**; at document level 5 wins, 0 losses, 59
ties. The result is not driven by one or two lucky questions.

Latency for a 30-document request: **p50 369 ms, p95 496 ms** on the local GPU, no API cost. The
blend has the same latency as pure BGE because it reuses the same scores.

The gate used for this decision was: MRR +0.05 or nDCG@10 +5 pp, recall@12 regression no worse
than 2 pp, and p95 under ~500 ms. The blend passes on MRR (+0.055 chunk, +0.059 section), recall
(−1.5 pp chunk, 0 at section/document), and p95 (496 ms). The nDCG gain is +3.0–3.7 pp, below the
5 pp alternative, so the MRR clause is what carries it.

## Reproduce

```bash
# 0. Start the BGE server (the physical batch must cover 30 documents)
llama-server \
  -m ~/.lmstudio/models/gpustack/bge-reranker-v2-m3-GGUF/bge-reranker-v2-m3-Q8_0.gguf \
  --reranking -b 4096 -ub 4096 --host 127.0.0.1 --port 8099 --no-webui

# 0b. Benchmark BGE at chunk, section and document relevance (read-only)
uv run python scripts/benchmark_bge_reranker.py --all \
  --out docs/reranker/bge-reranker-2026-11-11.json

# 1. Benchmark cosine vs zero-shot Laya (read-only)
uv run python scripts/benchmark_rerank.py \
  --out docs/reranker/benchmark-2026-11-11.json

# 2. Build the training/evaluation split from the real chat archive + golden set
uv run python scripts/build_rerank_training.py

# 3. Fine-tune (one device; the tested recipe is head-only)
laya-train --data docs/reranker/training/train-noul.jsonl \
           --eval docs/reranker/training/eval-noul.jsonl \
           --base convaiinnovations/laya \
           --out docs/reranker/checkpoints/laya-rerank-noul \
           --epochs 4 --micro-batch 1 --grad-accum 8 \
           --freeze-encoder --loss soft-ce --device cuda
```

## Decision

Keep the reranker **off by default**, with **BGE at blend 0.3** as the provider to enable when a
deployment wants a second stage:

- **Laya zero-shot and the head-only fine-tunes are rejected.** Both lose to cosine on every
  aggregate ranking metric.
- **Jev free is rejected for now.** It improves the four real-chat questions but loses on the
  synthetic golden majority, and paying for it is not justified by that sample.
- **Pure BGE is rejected.** It raises document-level ranking but costs 15 pp of chunk recall.
- **BGE at 0.3 is accepted.** It improves MRR and nDCG at chunk and section level, leaves section
  recall untouched, and adds ~370 ms p50 / ~500 ms p95 locally with no API cost.

The default stays off because 500 ms p95 is a real latency cost and the real-chat sample is still
only four questions. The admin card exposes the provider, model, and blend weight, so a deployment
can turn it on, tune the weight, and fall back to cosine by clearing the toggle.

Remaining experiments, in order:

1. A larger real-question set (50–100) from chat history, labelled at section/document level.
2. Benchmark `search_text`, where uniform match scores make the blend behave as pure BGE.
3. Ablate passage length (1,200 vs full), candidate pool (20/30/50), and BGE FP16 vs Q8.
4. Compare a learned fusion or RRF against the fixed 0.3 min-max blend.

## Jev 1.13 free (OpenCode Zen)

The free quota reset after the earlier `FreeUsageLimitError`, and the batched 16-query held-out
benchmark completed. It uses the deployment's **VLM API key** (an `oc_…` OpenCode Zen key) against
`https://opencode.ai/zen/v1/systemone` with `model = "jev-1.13-free"`.

- **48 batched calls** (10 candidates per request), **168,488 input tokens**, **8.9 s**, **cost 0**.
- Responses reported `billing_source: "free"` and `list_cost: 0`.
- Raw run: [`jev-free-eval-2026-11-11.json`](jev-free-eval-2026-11-11.json).

### Results (same 16 held-out questions as Laya)

| Set | Metric | Cosine | Jev free |
|---|---|---:|---:|
| All (16) | MRR | **0.516** | 0.485 |
| All (16) | nDCG@10 | **0.559** | 0.504 |
| All (16) | recall@12 | **93.8%** | 75.0% |
| Real chat (4) | MRR | 0.500 | **0.583** |
| Real chat (4) | nDCG@10 | 0.333 | **0.471** |
| Real chat (4) | recall@12 | **75.0%** | 75.0% |
| Golden (12) | MRR | **0.521** | 0.452 |
| Golden (12) | nDCG@10 | **0.634** | 0.515 |
| Golden (12) | recall@12 | **100%** | 75.0% |

Jev free is clearly stronger than Laya on this corpus (all-query MRR 0.485 vs Laya's 0.448), and it
**beats cosine on the four real chat questions** (MRR 0.583 vs 0.500, nDCG@10 0.471 vs 0.333). It
still loses to cosine on the twelve golden questions and on the combined set.

Four real questions is not enough to turn the feature on. At that point Jev free was the leading
external candidate; the BGE result above supersedes it for the default recommendation.

### Pointwise audit

The full run above was batched to stay inside the free call budget, but the production adapter is
pointwise. A separate pointwise audit of the four real chat questions (30 candidates each, 120
calls, 68,422 input tokens) scored:

| Set | MRR | nDCG@10 | recall@12 |
|---|---:|---:|---:|
| Chat, batched Jev free | 0.583 | 0.471 | 75% |
| **Chat, pointwise Jev free** | **0.625** | **0.480** | 75% |

So batching slightly understates Jev, but it does not change the direction of the result.

### Why the combined number is not the whole story

- **The combined set is 75% golden questions.** The real-chat subset is four questions; the
  twelve synthetic questions dominate the "all" row. On the real chat questions Jev beats cosine
  both batched and pointwise.
- **The golden labels are single-chunk.** A spot check showed Jev promoting an adjacent chunk from
  the same document and the same `Item 15` section as the labelled chunk (Activision ordinal 597
  vs Jev top 579), then ranking analogous registration/Item 10 sections from other companies.
  Those are often semantically reasonable but count as misses under a strict chunk label. A
  document- or section-level judgement would be fairer to any reranker.
- **Jev's score is coarse.** It frequently returns 0/1/2 with many ties at 2; ties keep the cosine
  order. Qwen3-Embedding-8B cosine is already strong (95% recall@12 on the full set, 100% on the
  golden held-out set), so there is little headroom and a few bad reorderings can dominate MRR.
- **The free model is the only Jev variant tested.** The paid `jev-1.13` was not called.

The harness is `scripts/benchmark_jev_free.py`; it batches 10 candidates per request to stay well
inside the free call budget and never falls back to the paid `jev-1.13` model. A full pointwise
run on a larger real-question set, with document/section-level relevance labels, is the next
experiment.
