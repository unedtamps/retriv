#!/usr/bin/env python
"""Benchmark OpenCode Zen's free Jev model as the retrieval reranker.

This is the same held-out split used for the Laya comparison: 4 real chat
questions and 12 golden questions, 30 cosine candidates each. Jev scores each
candidate pointwise through ``/v1/systemone``; the free model id is
``jev-1.13-free`` and the OpenCode Zen base URL is ``https://opencode.ai/zen/v1``.

    uv run python scripts/benchmark_jev_free.py

No paid model is called. If the free model is unavailable, the script exits
without falling back.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import Settings  # noqa: E402
from app.db.models import RetrievalSetting  # noqa: E402
from app.retrieval.context import ContextRetriever  # noqa: E402
from app.retrieval.schemas import ContextSource  # noqa: E402

ZEN_BASE = "https://opencode.ai/zen/v1"
FREE_MODEL = "jev-1.13-free"
CRITERIA = ["irrelevant", "partially relevant", "directly answers"]
MAX_CHARS = 1_200


def _location(source: ContextSource) -> str:
    location = ""
    if source.filename:
        location = f" ({source.filename}"
        if source.page_numbers:
            location += f", page {source.page_numbers[0]}"
        location += ")"
    return location


def _state(query: str, source: ContextSource) -> str:
    return f"Query: {query}\n\nPassage{_location(source)}:\n{source.text[:MAX_CHARS]}"


def _ranks(relevant: set[str], ordered: list[ContextSource]) -> list[int]:
    return [
        rank
        for rank, source in enumerate(ordered, start=1)
        if str(source.chunk_id) in relevant
    ]


def _ndcg(ranks: list[int], relevant: int, k: int = 10) -> float:
    gains = [1.0 / math.log2(rank + 1) for rank in ranks if rank <= k]
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(relevant, k)))
    return sum(gains) / ideal if ideal else 0.0


def _metrics(items: list[dict[str, object]]) -> dict[str, float]:
    mrr: list[float] = []
    ndcg: list[float] = []
    recall: list[float] = []
    for item in items:
        ranks = item["ranks"]  # type: ignore[assignment]
        mrr.append(1.0 / ranks[0] if ranks else 0.0)
        ndcg.append(_ndcg(ranks, int(item["relevant"])))
        recall.append(1.0 if any(rank <= 12 for rank in ranks) else 0.0)
    return {
        "mrr": statistics.fmean(mrr),
        "ndcg@10": statistics.fmean(ndcg),
        "recall@12": statistics.fmean(recall),
        "mean_first_rank": statistics.fmean(
            [r[0] for r in (item["ranks"] for item in items) if r]
        )
        if any(item["ranks"] for item in items)
        else 0.0,
    }


async def _score_batch(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    query: str,
    batch: list[ContextSource],
) -> tuple[dict[str, float], int]:
    async with semaphore:
        for attempt in range(5):
            response = await client.post(
                "/systemone",
                json={
                    "model": FREE_MODEL,
                    "state": f"Query: {query}\n\n"
                    + "\n\n".join(
                        f"Passage {number}{_location(source)}:\n{source.text[:MAX_CHARS]}"
                        for number, source in enumerate(batch, start=1)
                    ),
                    "questions": {
                        f"c{number}": {
                            "type": "score",
                            "instructions": (
                                f"Rate passage {number} for how directly it answers the query."
                            ),
                            "criteria": CRITERIA,
                        }
                        for number in range(1, len(batch) + 1)
                    },
                },
            )
            if response.status_code == 429:
                await asyncio.sleep(2**attempt)
                continue
            response.raise_for_status()
            payload = response.json()
            answers = payload.get("answers")
            if not isinstance(answers, dict):
                raise ValueError("Jev response carried no answers object")
            scores: dict[str, float] = {}
            for number, source in enumerate(batch, start=1):
                answer = answers.get(f"c{number}")
                if not isinstance(answer, dict):
                    raise ValueError(f"Jev response omitted passage {number}")
                scores[str(source.chunk_id)] = float(answer["score"])
            tokens = payload.get("usage", {}).get("input_tokens", 0)
            return scores, int(tokens)
    raise RuntimeError("Jev free returned 429 after five attempts")

async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    retriever = ContextRetriever(settings, engine)
    async with factory() as session:
        row = await session.get(RetrievalSetting, 1)
        if row is not None and row.rerank_enabled:
            row.rerank_enabled = False
            await session.commit()

    queries = json.loads(await asyncio.to_thread(Path(args.queries).read_text))
    if args.limit:
        queries = queries[: args.limit]

    semaphore = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(
        base_url=ZEN_BASE,
        headers={"Authorization": f"Bearer {settings.vlm_api_key.get_secret_value()}"},
        timeout=args.timeout,
    ) as client:
        items: list[dict[str, object]] = []
        started = time.perf_counter()
        tokens = 0
        calls = 0
        for index, entry in enumerate(queries, start=1):
            workspaces = [UUID(w) for w in entry["workspaces"]]
            candidates = await retriever.retrieve(
                entry["query"], workspaces, 30, budget=30
            )
            batches = [
                candidates[start : start + args.batch_size]
                for start in range(0, len(candidates), args.batch_size)
            ]
            outcomes = await asyncio.gather(
                *(_score_batch(client, semaphore, entry["query"], batch) for batch in batches)
            )
            calls += len(batches)
            by_id = {
                chunk_id: score
                for scores, _ in outcomes
                for chunk_id, score in scores.items()
            }
            tokens += sum(token_count for _, token_count in outcomes)
            ordered = sorted(
                candidates,
                key=lambda source: -by_id.get(str(source.chunk_id), 0.0),
            )
            relevant = set(entry["relevant"])
            items.append(
                {
                    "source": entry["source"],
                    "query": entry["query"],
                    "relevant": len(relevant),
                    "ranks": _ranks(relevant, ordered),
                    "cosine_ranks": _ranks(relevant, candidates),
                }
            )
            print(
                f"[{index}/{len(queries)}] {entry['source']:<6} "
                f"cosine={_ranks(relevant, candidates)[:1]} "
                f"jev={_ranks(relevant, ordered)[:1]}"
            )
        elapsed = time.perf_counter() - started

    summary = {
        "all": {
            "cosine": _metrics(
                [
                    {**item, "ranks": item["cosine_ranks"]}
                    for item in items
                ]
            ),
            "jev_free": _metrics(items),
        },
        "chat": {
            "cosine": _metrics(
                [
                    {**item, "ranks": item["cosine_ranks"]}
                    for item in items
                    if item["source"] == "chat"
                ]
            ),
            "jev_free": _metrics(
                [item for item in items if item["source"] == "chat"]
            ),
        },
        "golden": {
            "cosine": _metrics(
                [
                    {**item, "ranks": item["cosine_ranks"]}
                    for item in items
                    if item["source"] == "golden"
                ]
            ),
            "jev_free": _metrics(
                [item for item in items if item["source"] == "golden"]
            ),
        },
    }
    payload = {
        "model": FREE_MODEL,
        "base_url": ZEN_BASE,
        "queries": len(items),
        "candidates": 30,
        "concurrency": args.concurrency,
        "batch_size": args.batch_size,
        "elapsed_seconds": elapsed,
        "calls": calls,
        "input_tokens": tokens,
        "cost": 0,
        "summary": summary,
        "items": items,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(out.write_text, json.dumps(payload, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    print(f"{calls} calls, {tokens} input tokens, {elapsed:.1f}s -> {out}")

    await retriever.aclose()
    await engine.dispose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--queries",
        default="docs/reranker/training/eval_queries.json",
        help="Held-out query file (default: the Laya comparison split).",
    )
    parser.add_argument("--out", default="docs/reranker/jev-free-eval-2026-11-11.json")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--limit", type=int, default=None)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
