#!/usr/bin/env python
"""Benchmark bge-reranker-v2-m3 against cosine at three relevance levels.

The strict chunk-level label used in the earlier Laya/Jev runs is too harsh
for a reranker: a neighbouring chunk in the same section, or another passage
in the same document, may answer the question just as well. This harness
reports chunk-, section- and document-level recall and ranking metrics.

The reranker is the local llama.cpp server:

    llama-server \
      -m ~/.lmstudio/models/gpustack/bge-reranker-v2-m3-GGUF/bge-reranker-v2-m3-Q8_0.gguf \
      --reranking -b 4096 -ub 4096 --host 127.0.0.1 --port 8099 --no-webui

    uv run python scripts/benchmark_bge_reranker.py --all \
      --out docs/reranker/bge-reranker-2026-11-11.json

The script is read-only: it retrieves candidates through the production
``ContextRetriever`` and writes only the benchmark JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import Settings  # noqa: E402
from app.db.models import DocumentChunk, RetrievalSetting  # noqa: E402
from app.retrieval.context import ContextRetriever  # noqa: E402
from app.retrieval.schemas import ContextSource  # noqa: E402

MAX_CHARS = 1_200
BLEND_WEIGHT = 0.3
LEVELS = ("chunk", "section", "document")


@dataclass(frozen=True)
class Query:
    query: str
    relevant: frozenset[str]
    workspaces: tuple[UUID, ...]
    source: str


@dataclass(frozen=True)
class Label:
    chunk_id: str
    document_id: str
    section_path: tuple[str, ...]


def _load_queries(args: argparse.Namespace) -> list[Query]:
    queries: list[Query] = []
    if args.heldout:
        for entry in json.loads(Path(args.heldout).read_text()):
            queries.append(
                Query(
                    query=entry["query"],
                    relevant=frozenset(entry["relevant"]),
                    workspaces=tuple(UUID(w) for w in entry["workspaces"]),
                    source=entry["source"],
                )
            )
        return queries

    for entry in json.loads(Path(args.chat_queries).read_text())["queries"]:
        relevant = frozenset(
            c["chunk_id"] for c in entry["citations"] if c.get("chunk_id")
        )
        workspaces = {
            UUID(c["workspace_id"]) for c in entry["citations"] if c.get("workspace_id")
        }
        if relevant and workspaces:
            queries.append(
                Query(entry["query"], relevant, tuple(sorted(workspaces)), "chat")
            )
    for entry in json.loads(Path(args.golden).read_text())["queries"]:
        if entry.get("chunk_id") and entry.get("workspace_id"):
            queries.append(
                Query(
                    entry["query"],
                    frozenset({entry["chunk_id"]}),
                    (UUID(entry["workspace_id"]),),
                    "golden",
                )
            )
    return queries


def _section_matches(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    if not left or not right:
        return left == right
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    return longer[: len(shorter)] == shorter


def _is_relevant(source: ContextSource, labels: list[Label], level: str) -> bool:
    if level == "chunk":
        return any(str(source.chunk_id) == label.chunk_id for label in labels)
    if level == "document":
        return any(str(source.document_id) == label.document_id for label in labels)
    path = tuple(source.section_path)
    return any(
        str(source.document_id) == label.document_id
        and _section_matches(path, label.section_path)
        for label in labels
    )


def _normalize(values: list[float]) -> list[float]:
    low, high = min(values), max(values)
    if high == low:
        return [0.5] * len(values)
    return [(value - low) / (high - low) for value in values]


def _blend_order(
    sources: list[ContextSource],
    rerank_scores: list[float],
    weight: float,
) -> list[ContextSource]:
    """Min-max blend cosine and BGE, as the production adapter does."""
    cosine = _normalize([source.score for source in sources])
    rerank = _normalize(rerank_scores)
    keyed = [
        ((1.0 - weight) * cosine_part + weight * rerank_part, index)
        for index, (cosine_part, rerank_part) in enumerate(
            zip(cosine, rerank, strict=True)
        )
    ]
    return [
        sources[index]
        for _, index in sorted(keyed, key=lambda item: (-item[0], item[1]))
    ]


def _ranks(
    ordered: list[ContextSource], labels: list[Label], level: str
) -> list[int]:
    return [
        rank
        for rank, source in enumerate(ordered, start=1)
        if _is_relevant(source, labels, level)
    ]


def _ndcg(
    ranks: list[int],
    pool: list[ContextSource],
    labels: list[Label],
    level: str,
    k: int,
) -> float:
    gains = [1.0 / math.log2(rank + 1) for rank in ranks if rank <= k]
    relevant = sum(1 for source in pool if _is_relevant(source, labels, level))
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(relevant, k)))
    return sum(gains) / ideal if ideal else 0.0


def _summary(
    items: list[dict[str, object]],
    level: str,
) -> dict[str, float]:
    total = len(items) or 1
    mrr = [
        1.0 / item["ranks"][level][0] if item["ranks"][level] else 0.0  # type: ignore[index]
        for item in items
    ]
    ndcg = [
        _ndcg(
            item["ranks"][level],  # type: ignore[index]
            item["pool"],  # type: ignore[arg-type]
            item["labels"],  # type: ignore[arg-type]
            level,
            10,
        )
        for item in items
    ]
    recall = [
        1.0 if any(rank <= 12 for rank in item["ranks"][level]) else 0.0  # type: ignore[index]
        for item in items
    ]
    return {
        "queries": len(items),
        "mrr": statistics.fmean(mrr) if mrr else 0.0,
        "ndcg@10": statistics.fmean(ndcg) if ndcg else 0.0,
        "recall@12": sum(recall) / total,
    }


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

    queries = _load_queries(args)
    label_ids = {chunk for query in queries for chunk in query.relevant}
    labels_by_chunk: dict[str, Label] = {}
    async with factory() as session:
        rows = (
            await session.execute(
                select(
                    DocumentChunk.id,
                    DocumentChunk.document_id,
                    DocumentChunk.section_path,
                ).where(DocumentChunk.id.in_([UUID(chunk) for chunk in label_ids]))
            )
        ).all()
        labels_by_chunk = {
            str(row.id): Label(
                chunk_id=str(row.id),
                document_id=str(row.document_id),
                section_path=tuple(row.section_path or []),
            )
            for row in rows
        }

    async with httpx.AsyncClient(base_url=args.rerank_url, timeout=args.timeout) as client:
        items: list[dict[str, object]] = []
        latencies: list[float] = []
        for index, query in enumerate(queries, start=1):
            labels = [
                labels_by_chunk[chunk]
                for chunk in query.relevant
                if chunk in labels_by_chunk
            ]
            if not labels:
                continue
            candidates = await retriever.retrieve(
                query.query, list(query.workspaces), args.candidates, budget=args.candidates
            )
            t0 = time.perf_counter()
            response = await client.post(
                "/v1/rerank",
                json={
                    "model": args.model,
                    "query": query.query,
                    "documents": [source.text[:MAX_CHARS] for source in candidates],
                },
            )
            response.raise_for_status()
            latency = (time.perf_counter() - t0) * 1000
            latencies.append(latency)
            scores = {
                entry["index"]: entry["relevance_score"]
                for entry in response.json()["results"]
            }
            ordered = [
                source
                for _, source in sorted(
                    enumerate(candidates),
                    key=lambda pair: -scores.get(pair[0], -1e9),
                )
            ]
            ranks = {
                level: _ranks(ordered, labels, level) for level in LEVELS
            }
            cosine_ranks = {
                level: _ranks(candidates, labels, level) for level in LEVELS
            }
            bge_scores = [
                scores.get(index, -1e9) for index in range(len(candidates))
            ]
            blended = _blend_order(candidates, bge_scores, BLEND_WEIGHT)
            blend_ranks = {
                level: _ranks(blended, labels, level) for level in LEVELS
            }
            items.append(
                {
                    "source": query.source,
                    "query": query.query,
                    "pool": candidates,
                    "labels": labels,
                    "ranks": ranks,
                    "cosine_ranks": cosine_ranks,
                    "blend_ranks": blend_ranks,
                    "bge_scores": bge_scores,
                }
            )
            print(
                f"[{index}/{len(queries)}] {query.source:<6} "
                f"chunk={ranks['chunk'][:1]} section={ranks['section'][:1]} "
                f"doc={ranks['document'][:1]} {latency:.0f} ms"
            )

    summary: dict[str, object] = {}
    for name, subset in {
        "all": items,
        "chat": [item for item in items if item["source"] == "chat"],
        "golden": [item for item in items if item["source"] == "golden"],
    }.items():
        summary[name] = {
            "cosine": {
                level: _summary(
                    [
                        {**item, "ranks": item["cosine_ranks"]}
                        for item in subset
                    ],
                    level,
                )
                for level in LEVELS
            },
            "bge": {level: _summary(subset, level) for level in LEVELS},
            f"blend_{BLEND_WEIGHT}": {
                level: _summary(
                    [{**item, "ranks": item["blend_ranks"]} for item in subset],
                    level,
                )
                for level in LEVELS
            },
        }

    payload = {
        "model": args.model,
        "rerank_url": args.rerank_url,
        "candidates": args.candidates,
        "queries": len(items),
        "latency_ms": {
            "p50": statistics.median(latencies) if latencies else None,
            "p95": sorted(latencies)[max(0, math.ceil(len(latencies) * 0.95) - 1)]
            if latencies
            else None,
        },
        "summary": summary,
        "items": [
            {
                "source": item["source"],
                "query": item["query"],
                "ranks": item["ranks"],
                "cosine_ranks": item["cosine_ranks"],
                "blend_ranks": item["blend_ranks"],
            }
            for item in items
        ],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(out.write_text, json.dumps(payload, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    print(f"wrote {out}")

    if args.orders_out:
        orders = [
            {
                "source": item["source"],
                "query": item["query"],
                "labels": [
                    {
                        "chunk_id": label.chunk_id,
                        "document_id": label.document_id,
                        "section_path": list(label.section_path),
                    }
                    for label in item["labels"]  # type: ignore[union-attr]
                ],
                "candidates": [
                    {
                        "chunk_id": str(source.chunk_id),
                        "document_id": str(source.document_id),
                        "section_path": list(source.section_path),
                        "cosine": source.score,
                        "bge": item["bge_scores"][index],  # type: ignore[index]
                    }
                    for index, source in enumerate(item["pool"])  # type: ignore[arg-type]
                ],
            }
            for item in items
        ]
        orders_out = Path(args.orders_out)
        orders_out.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            orders_out.write_text, json.dumps(orders, indent=2) + "\n"
        )
        print(f"wrote {orders_out}")
    await retriever.aclose()
    await engine.dispose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--heldout", default="docs/reranker/training/eval_queries.json")
    parser.add_argument("--all", action="store_true", help="Use the full chat + golden set.")
    parser.add_argument("--chat-queries", default="evals/rerank_chat_queries.json")
    parser.add_argument("--golden", default="evals/retrieval_golden.json")
    parser.add_argument("--rerank-url", default="http://127.0.0.1:8099")
    parser.add_argument("--model", default="bge-reranker-v2-m3")
    parser.add_argument("--candidates", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--out", default="docs/reranker/bge-reranker-2026-11-11.json")
    parser.add_argument(
        "--orders-out",
        default="",
        help="Optional path for per-candidate cosine and BGE scores.",
    )
    args = parser.parse_args()
    if args.all:
        args.heldout = ""
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
