# Laya reranker: optional second stage

**Status:** implemented, **disabled by default**, and **not recommended for production yet**.
The benchmark and fine-tuning experiment below explain why.

This folder contains the reproducible benchmark, the fine-tuning dataset recipe, and the
comparison that led to the decision to keep cosine retrieval as the hot path.

## What was built

`search_context` and `search_text` can now pass their candidate pool through an optional Laya
reranker before the agent sees the results.

- **Admin-controlled, global, off by default.** Configure it in **Platform administration →
  Agent harness → Retrieval → Reranking**.
- **Candidate pool:** 30 cosine candidates by default.
- **Final context:** the existing cap (`max_context_sources = 12`, section completion included).
- **Scoring:** each candidate is sent pointwise to Laya's `/v1/systemone` as a `score` question
  with the rubric `irrelevant / partially relevant / directly answers`.
- **Concurrency:** 8 pointwise requests by default.
- **Fail open:** a timeout, non-200 response, or malformed answer returns the original cosine
  order and logs a warning. The agent never loses results because the reranker is down.
- **Scope:** only the two search tools. `read_document` and `query_rows` keep their existing
  behaviour.

The implementation lives in:

- `app/retrieval/rerank.py` — Laya adapter, scoring, concurrency, fallback.
- `app/retrieval/context.py` — candidate over-fetch and rerank for both search paths.
- `app/routes/admin.py` — `GET/PUT /v1/admin/retrieval` and `POST /v1/admin/retrieval/test`.
- `web/src/components/AdminRerank.tsx` — the admin card.
- `alembic/versions/0004_retrieval_settings.py` — the singleton settings table.

## LM Studio requirements

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

## Benchmark

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

## Reproduce

```bash
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

Keep the reranker **off**. Cosine Qwen3 retrieval remains the production path. The admin toggle,
adapter, and benchmark stay in place so a future model or a better fine-tune can be evaluated
without changing the hot path. If this is revisited, the next experiments should be:

1. A larger, cleaner labelled set, ideally with real user questions and graded relevance; Jev free
   is the leading candidate to retest on it.
2. Same-document hard negatives to remove document-name shortcuts.
3. Full encoder fine-tuning (not head-only) or a cross-encoder such as `bge-reranker-v2-m3`.
4. A two-stage blend rather than replacing cosine order.

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

Four real questions is not enough to turn the feature on. The signal is promising enough to keep
Jev free as the leading external candidate, but the default stays off until a larger real-question
set confirms the chat result.

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
