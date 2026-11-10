#!/usr/bin/env python
"""Compare cosine retrieval with the optional Laya reranker.

This is a read-only evaluation: it pulls candidates through the same
``ContextRetriever`` the tools use, then sends the candidate pool to Laya's
``/v1/systemone`` endpoint pointwise. The output is the baseline-versus-rerank
table used in ``docs/reranker/README.md``.

    uv run python scripts/benchmark_rerank.py \
        --chat-queries evals/rerank_chat_queries.json \
        --golden evals/retrieval_golden.json \
        --out docs/reranker/benchmark-2026-11-11.json

The chat questions carry the chunks the assistant actually cited as labels;
the golden set carries the chunk each question was generated from. The script
does not write to the database: it temporarily turns the admin rerank flag off
for the baseline and restores it on exit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import Settings  # noqa: E402
from app.db.models import RetrievalSetting  # noqa: E402
from app.retrieval.context import ContextRetriever  # noqa: E402
from app.retrieval.rerank import LayaReranker, RerankConfig  # noqa: E402
from app.retrieval.schemas import ContextSource  # noqa: E402


@dataclass(frozen=True)
class Query:
    query: str
    relevant: set[str]
    workspace_ids: list[UUID]
    source: str


@dataclass
class QueryOutcome:
    source: str
    query: str
    relevant: int
    baseline_rank: int | None
    rerank_rank: int | None
    baseline_ndcg: float
    rerank_ndcg: float
    baseline_recall: dict[int, bool]
    rerank_recall: dict[int, bool]
    candidate_miss: bool
    fallback: bool
    baseline_ms: float
    rerank_ms: float
    calls: int
    input_tokens: int


def _load_chat(path: Path) -> list[Query]:
    payload = json.loads(path.read_text())
    queries: list[Query] = []
    for entry in payload.get("queries", []):
        relevant = {c["chunk_id"] for c in entry["citations"] if c.get("chunk_id")}
        workspaces = {
            UUID(c["workspace_id"]) for c in entry["citations"] if c.get("workspace_id")
        }
        if relevant and workspaces:
            queries.append(
                Query(
                    query=entry["query"],
                    relevant=relevant,
                    workspace_ids=sorted(workspaces),
                    source="chat",
                )
            )
    return queries


def _load_golden(path: Path) -> list[Query]:
    payload = json.loads(path.read_text())
    queries: list[Query] = []
    for entry in payload.get("queries", []):
        if entry.get("chunk_id") and entry.get("workspace_id"):
            queries.append(
                Query(
                    query=entry["query"],
                    relevant={entry["chunk_id"]},
                    workspace_ids=[UUID(entry["workspace_id"])],
                    source="golden",
                )
            )
    return queries


def _ranks(relevant: set[str], ordered: list[ContextSource]) -> list[int]:
    return [
        rank
        for rank, source in enumerate(ordered, start=1)
        if str(source.chunk_id) in relevant
    ]


def _ndcg(ranks: list[int], relevant: int, k: int) -> float:
    gains = [1.0 / math.log2(rank + 1) for rank in ranks if rank <= k]
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(relevant, k)))
    return sum(gains) / ideal if ideal else 0.0


def _recall(ranks: list[int]) -> dict[int, bool]:
    return {k: any(rank <= k for rank in ranks) for k in (1, 5, 10, 12)}


def _summary(outcomes: list[QueryOutcome]) -> dict[str, object]:
    total = len(outcomes) or 1
    baseline_ranks = [o.baseline_rank for o in outcomes if o.baseline_rank is not None]
    rerank_ranks = [o.rerank_rank for o in outcomes if o.rerank_rank is not None]
    baseline_ms = [o.baseline_ms for o in outcomes]
    rerank_ms = [o.rerank_ms for o in outcomes]
    return {
        "queries": len(outcomes),
        "candidate_misses": sum(1 for o in outcomes if o.candidate_miss),
        "fallbacks": sum(1 for o in outcomes if o.fallback),
        "baseline": {
            "recall@1": sum(1 for o in outcomes if o.baseline_recall[1]) / total,
            "recall@5": sum(1 for o in outcomes if o.baseline_recall[5]) / total,
            "recall@10": sum(1 for o in outcomes if o.baseline_recall[10]) / total,
            "recall@12": sum(1 for o in outcomes if o.baseline_recall[12]) / total,
            "mrr": sum(1.0 / r for r in baseline_ranks) / total,
            "ndcg@10": sum(o.baseline_ndcg for o in outcomes) / total,
            "mean_first_rank": statistics.fmean(baseline_ranks) if baseline_ranks else None,
            "p50_ms": statistics.median(baseline_ms) if baseline_ms else None,
            "p95_ms": _p95(baseline_ms),
        },
        "rerank": {
            "recall@1": sum(1 for o in outcomes if o.rerank_recall[1]) / total,
            "recall@5": sum(1 for o in outcomes if o.rerank_recall[5]) / total,
            "recall@10": sum(1 for o in outcomes if o.rerank_recall[10]) / total,
            "recall@12": sum(1 for o in outcomes if o.rerank_recall[12]) / total,
            "mrr": sum(1.0 / r for r in rerank_ranks) / total,
            "ndcg@10": sum(o.rerank_ndcg for o in outcomes) / total,
            "mean_first_rank": statistics.fmean(rerank_ranks) if rerank_ranks else None,
            "p50_ms": statistics.median(rerank_ms) if rerank_ms else None,
            "p95_ms": _p95(rerank_ms),
            "calls_per_query": statistics.fmean(o.calls for o in outcomes),
            "tokens_per_query": statistics.fmean(o.input_tokens for o in outcomes),
        },
    }


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)
    return ordered[index]


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    retriever = ContextRetriever(settings, engine)
    client = httpx.AsyncClient(
        base_url=f"{settings.embedding_base_url.rstrip('/')}/",
        headers={
            "Authorization": f"Bearer {settings.embedding_api_key.get_secret_value()}"
        },
        timeout=max(30.0, args.timeout_ms / 1000),
    )

    previous_enabled = False
    async with factory() as session:
        row = await session.get(RetrievalSetting, 1)
        if row is not None:
            previous_enabled = row.rerank_enabled
            row.rerank_enabled = False
            await session.commit()

    queries = _load_chat(Path(args.chat_queries)) + _load_golden(Path(args.golden))
    if args.limit:
        queries = queries[: args.limit]
    if not queries:
        print("No labeled queries found.", file=sys.stderr)
        return 2

    config = RerankConfig(
        model=args.model,
        candidates=args.candidates,
        concurrency=args.concurrency,
        min_score=args.min_score,
        timeout_ms=args.timeout_ms,
    )
    outcomes: list[QueryOutcome] = []
    try:
        for index, query in enumerate(queries, start=1):
            baseline_started = perf_counter()
            candidates = await retriever.retrieve(
                query.query,
                query.workspace_ids,
                args.candidates,
                budget=args.candidates,
            )
            baseline_ms = (perf_counter() - baseline_started) * 1000
            baseline = candidates[: args.top_n]
            baseline_ranks = _ranks(query.relevant, baseline)

            rerank_started = perf_counter()
            result = await LayaReranker(client, config).rerank(
                query.query, candidates, limit=args.top_n
            )
            rerank_ms = (perf_counter() - rerank_started) * 1000
            rerank = result.sources
            rerank_ranks = _ranks(query.relevant, rerank)
            outcome = QueryOutcome(
                source=query.source,
                query=query.query,
                relevant=len(query.relevant),
                baseline_rank=baseline_ranks[0] if baseline_ranks else None,
                rerank_rank=rerank_ranks[0] if rerank_ranks else None,
                baseline_ndcg=_ndcg(baseline_ranks, len(query.relevant), 10),
                rerank_ndcg=_ndcg(rerank_ranks, len(query.relevant), 10),
                baseline_recall=_recall(baseline_ranks),
                rerank_recall=_recall(rerank_ranks),
                candidate_miss=not _ranks(query.relevant, candidates),
                fallback=result.fallback,
                baseline_ms=baseline_ms,
                rerank_ms=rerank_ms,
                calls=result.calls,
                input_tokens=result.input_tokens,
            )
            outcomes.append(outcome)
            print(
                f"[{index:>2}/{len(queries)}] {query.source:<6} "
                f"base={outcome.baseline_rank or '-':>3} "
                f"rerank={outcome.rerank_rank or '-':>3} "
                f"{outcome.rerank_ms:6.0f} ms  {query.query[:60]}"
            )
    finally:
        async with factory() as session:
            row = await session.get(RetrievalSetting, 1)
            if row is not None:
                row.rerank_enabled = previous_enabled
                await session.commit()
        await client.aclose()
        await retriever.aclose()
        await engine.dispose()

    summary = {
        "all": _summary(outcomes),
        "chat": _summary([o for o in outcomes if o.source == "chat"]),
        "golden": _summary([o for o in outcomes if o.source == "golden"]),
    }
    payload = {
        "ran_at": datetime.now(UTC).isoformat(),
        "config": {
            "embedding_model": settings.embedding_model,
            "rerank_model": args.model,
            "candidates": args.candidates,
            "top_n": args.top_n,
            "concurrency": args.concurrency,
            "min_score": args.min_score,
            "timeout_ms": args.timeout_ms,
        },
        "summary": summary,
        "queries": [asdict(o) for o in outcomes],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(out.write_text, json.dumps(payload, indent=2) + "\n")

    for name, values in summary.items():
        base = values["baseline"]
        rerank = values["rerank"]
        print(
            f"\n{name}: n={values['queries']} "
            f"baseline MRR={base['mrr']:.3f} nDCG@10={base['ndcg@10']:.3f} "
            f"recall@12={base['recall@12']:.0%} p50={base['p50_ms']:.0f} ms"
        )
        print(
            f"{'':<{len(name)}}  rerank  MRR={rerank['mrr']:.3f} "
            f"nDCG@10={rerank['ndcg@10']:.3f} recall@12={rerank['recall@12']:.0%} "
            f"p50={rerank['p50_ms']:.0f} ms calls/q={rerank['calls_per_query']:.1f} "
            f"tokens/q={rerank['tokens_per_query']:.0f}"
        )
    print(f"\nWrote {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-queries", default="evals/rerank_chat_queries.json")
    parser.add_argument("--golden", default="evals/retrieval_golden.json")
    parser.add_argument("--out", default="docs/reranker/benchmark.json")
    parser.add_argument("--model", default="laya")
    parser.add_argument("--candidates", type=int, default=30)
    parser.add_argument("--top-n", type=int, default=12)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("--timeout-ms", type=int, default=10_000)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
