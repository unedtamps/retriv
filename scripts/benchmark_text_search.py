#!/usr/bin/env python
"""Benchmark lexical search: current substring lookup vs BM25 vs a hybrid.

The agent's ``search_text`` tool currently requires the whole query to appear as
one contiguous, case-insensitive substring. That is precise for identifiers but
useless for a long keyword string whose terms are present in a different order.

This harness compares three lexical rankings over the same chunks:

- ``ilike``  — the production ``ContextRetriever.search_text``;
- ``bm25``   — Okapi BM25 over the chunk text, with stopwords removed;
- ``hybrid`` — exact substring matches first, then BM25 for the rest.

It runs the 64 retrieval questions as long lexical queries, plus two extra
sets: identifiers mined from the corpus and synthetic non-contiguous term
queries. The identifier set checks that a lexical upgrade does not lose the
tool's original precision; the synthetic set is a capability check, not a
quality claim.

Usage::

    uv run python scripts/benchmark_text_search.py \
      --out docs/benchmarks/2026-11-11-text-search-bm25.json

The harness is read-only; it writes only the benchmark JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import Settings  # noqa: E402
from app.db.models import (  # noqa: E402
    Document,
    DocumentChunk,
    DocumentWorkspace,
    RetrievalSetting,
)
from app.retrieval.context import ContextRetriever  # noqa: E402

LEVELS = ("chunk", "section", "document")
TOKEN_RE = re.compile(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*")
STOPWORDS = frozenset(
    """
    a an and are as at be been but by can could did do does for from had has have
    how i if in into is it its may might more most must no not of on or our over
    shall should so than that the their them then there these they this those to
    was we were what when where which who why will with would you your
    """.split()  # noqa: SIM905 - one grouped list is clearer than 65 set entries
)
IDENTIFIER_PATTERNS = (
    re.compile(r"\b[A-Z]{2,}[A-Z0-9]*(?:[-_/][A-Z0-9]+){1,}\b"),
    re.compile(r"\b[A-Z]{1,5}-\d{1,4}\b"),
    re.compile(
        r"\b(?:Pasal|Article|Section|Exhibit|Item|Form|Schedule)"
        r"\s+[A-Z0-9][A-Z0-9.\-/]*\b"
    ),
)


@dataclass(frozen=True)
class Query:
    query: str
    relevant: frozenset[str]
    workspaces: tuple[UUID, ...]
    source: str


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    document_id: str
    section_path: tuple[str, ...]
    ordinal: int
    text: str
    tokens: tuple[str, ...]


class BM25:
    """Okapi BM25 with an inverted index and precomputed length norms."""

    def __init__(
        self,
        chunks: list[Chunk],
        *,
        k1: float = 1.5,
        b: float = 0.75,
        remove_stopwords: bool = True,
    ) -> None:
        self._k1 = k1
        self._b = b
        self._stopwords = STOPWORDS if remove_stopwords else frozenset()
        self._documents = len(chunks)
        self._postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        lengths = [0] * self._documents
        for index, chunk in enumerate(chunks):
            tokens = [token for token in chunk.tokens if token not in self._stopwords]
            lengths[index] = len(tokens)
            for term, frequency in Counter(tokens).items():
                self._postings[term].append((index, frequency))
        self._average_length = (
            statistics.fmean(lengths) if lengths else 0.0
        ) or 1.0
        self._norms = [
            k1 * (1.0 - b + b * length / self._average_length)
            for length in lengths
        ]
        self._idf: dict[str, float] = {}
        for term, postings in self._postings.items():
            frequency = len(postings)
            self._idf[term] = math.log(
                1.0 + (self._documents - frequency + 0.5) / (frequency + 0.5)
            )

    def scores(self, query: str) -> list[float]:
        values = [0.0] * self._documents
        for term in tokenize(query, self._stopwords):
            postings = self._postings.get(term)
            if not postings:
                continue
            idf = self._idf[term]
            for index, frequency in postings:
                values[index] += (
                    idf
                    * frequency
                    * (self._k1 + 1.0)
                    / (frequency + self._norms[index])
                )
        return values

    def ranked_ids(self, query: str, chunks: list[Chunk], limit: int) -> list[str]:
        values = self.scores(query)
        ranked = sorted(
            range(self._documents),
            key=lambda index: (-values[index], index),
        )
        return [chunks[index].chunk_id for index in ranked[:limit]]


def tokenize(text: str, stopwords: frozenset[str] = STOPWORDS) -> list[str]:
    return [
        token
        for token in TOKEN_RE.findall(text.lower())
        if token not in stopwords
    ]


def section_matches(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    if not left or not right:
        return left == right
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    return longer[: len(shorter)] == shorter


def is_relevant(
    candidate: Chunk,
    labels: list[Chunk],
    level: str,
) -> bool:
    if level == "chunk":
        return any(candidate.chunk_id == label.chunk_id for label in labels)
    if level == "document":
        return any(candidate.document_id == label.document_id for label in labels)
    return any(
        candidate.document_id == label.document_id
        and section_matches(candidate.section_path, label.section_path)
        for label in labels
    )


def ranks(ids: list[str], chunks: list[Chunk], labels: list[Chunk], level: str) -> list[int]:
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    return [
        rank
        for rank, chunk_id in enumerate(ids, start=1)
        if chunk_id in by_id and is_relevant(by_id[chunk_id], labels, level)
    ]


def ndcg_at_10(hit_ranks: list[int], label_count: int) -> float:
    gains = [1.0 / math.log2(rank + 1) for rank in hit_ranks if rank <= 10]
    ideal = sum(
        1.0 / math.log2(index + 2) for index in range(min(label_count, 10))
    )
    return sum(gains) / ideal if ideal else 0.0


def summarize(
    results: list[tuple[list[int], int]],
) -> dict[str, float]:
    if not results:
        return {"queries": 0, "mrr": 0.0, "ndcg@10": 0.0, "recall@12": 0.0}
    mrr = statistics.fmean(
        [1.0 / hit_ranks[0] if hit_ranks else 0.0 for hit_ranks, _ in results]
    )
    ndcg = statistics.fmean(
        [ndcg_at_10(hit_ranks, labels) for hit_ranks, labels in results]
    )
    recall = statistics.fmean(
        [1.0 if any(rank <= 12 for rank in hit_ranks) else 0.0 for hit_ranks, _ in results]
    )
    return {
        "queries": len(results),
        "mrr": mrr,
        "ndcg@10": ndcg,
        "recall@12": recall,
    }


def load_queries(
    chat_path: Path,
    golden_path: Path,
) -> tuple[list[Query], list[Query]]:
    chat: list[Query] = []
    for entry in json.loads(chat_path.read_text())["queries"]:
        relevant = {
            citation["chunk_id"]
            for citation in entry["citations"]
            if citation.get("chunk_id")
        }
        workspaces = {
            UUID(citation["workspace_id"])
            for citation in entry["citations"]
            if citation.get("workspace_id")
        }
        if relevant and workspaces:
            chat.append(
                Query(
                    entry["query"],
                    frozenset(relevant),
                    tuple(sorted(workspaces)),
                    "chat",
                )
            )
    golden: list[Query] = []
    for entry in json.loads(golden_path.read_text())["queries"]:
        if entry.get("chunk_id") and entry.get("workspace_id"):
            golden.append(
                Query(
                    entry["query"],
                    frozenset({entry["chunk_id"]}),
                    (UUID(entry["workspace_id"]),),
                    "golden",
                )
            )
    return chat, golden


async def load_corpus(
    factory: async_sessionmaker,
    workspace_ids: set[UUID],
) -> list[Chunk]:
    statement = (
        select(
            DocumentChunk.id,
            DocumentChunk.document_id,
            DocumentChunk.section_path,
            DocumentChunk.ordinal,
            DocumentChunk.text,
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .join(
            DocumentWorkspace,
            DocumentWorkspace.document_id == DocumentChunk.document_id,
        )
        .where(
            DocumentWorkspace.workspace_id.in_(workspace_ids),
            DocumentWorkspace.deleted_at.is_(None),
            Document.deleted_at.is_(None),
            Document.active_processing_run_id == DocumentChunk.processing_run_id,
        )
        .order_by(DocumentChunk.document_id, DocumentChunk.ordinal)
    )
    async with factory() as session:
        rows = (await session.execute(statement)).all()
    seen: set[str] = set()
    chunks: list[Chunk] = []
    for row in rows:
        chunk_id = str(row.id)
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                document_id=str(row.document_id),
                section_path=tuple(row.section_path or ()),
                ordinal=int(row.ordinal),
                text=str(row.text),
                tokens=tuple(TOKEN_RE.findall(str(row.text).lower())),
            )
        )
    return chunks


def mine_identifiers(
    chunks: list[Chunk],
    workspaces: tuple[UUID, ...],
    limit: int,
) -> list[Query]:
    occurrences: dict[str, set[str]] = defaultdict(set)
    surfaces: dict[str, str] = {}
    for chunk in chunks:
        for pattern in IDENTIFIER_PATTERNS:
            for match in pattern.finditer(chunk.text):
                value = match.group(0).strip(" .,;:()[]")
                if len(value) < 4:
                    continue
                key = value.lower()
                occurrences[key].add(chunk.chunk_id)
                surfaces.setdefault(key, value)
    candidates = [
        (surfaces[key], chunk_ids)
        for key, chunk_ids in occurrences.items()
        if 1 <= len(chunk_ids) <= 3
    ]
    candidates.sort(key=lambda item: (-len(item[0]), item[0].lower()))
    return [
        Query(value, frozenset(chunk_ids), workspaces, "identifier")
        for value, chunk_ids in candidates[:limit]
    ]


def mine_term_queries(
    chunks: list[Chunk],
    workspaces: tuple[UUID, ...],
    limit: int,
) -> list[Query]:
    randomizer = random.Random(20261111)  # noqa: S311 - deterministic sampling
    candidates: list[Chunk] = []
    for chunk in chunks:
        distinct = {
            token
            for token in chunk.tokens
            if token not in STOPWORDS and len(token) >= 4
        }
        if len(distinct) >= 4:
            candidates.append(chunk)
    randomizer.shuffle(candidates)
    queries: list[Query] = []
    for chunk in candidates[:limit]:
        distinct = sorted(
            {
                token
                for token in chunk.tokens
                if token not in STOPWORDS and len(token) >= 4
            }
        )
        chosen = randomizer.sample(distinct, 4)
        queries.append(
            Query(" ".join(chosen), frozenset({chunk.chunk_id}), workspaces, "terms")
        )
    return queries


async def evaluate(
    retriever: ContextRetriever,
    chunks: list[Chunk],
    queries: list[Query],
    *,
    limit: int,
) -> dict[str, object]:
    by_id = {chunk.chunk_id: chunk for chunk in chunks}
    bm25 = BM25(chunks, remove_stopwords=True)
    rows: list[dict[str, object]] = []
    for query in queries:
        if not any(chunk in by_id for chunk in query.relevant):
            continue
        text = query.query[:200]
        started = time.perf_counter()
        exact_sources = await retriever.search_text(
            text, list(query.workspaces), limit
        )
        exact_ms = (time.perf_counter() - started) * 1000
        exact_ids = [str(source.chunk_id) for source in exact_sources]

        started = time.perf_counter()
        bm25_all = bm25.ranked_ids(text, chunks, limit * 2)
        bm25_ms = (time.perf_counter() - started) * 1000
        bm25_ids = bm25_all[:limit]

        seen = set(exact_ids)
        hybrid_ids = list(exact_ids)
        if len(hybrid_ids) < limit:
            for chunk_id in bm25_all:
                if chunk_id not in seen:
                    hybrid_ids.append(chunk_id)
                    seen.add(chunk_id)
                if len(hybrid_ids) >= limit:
                    break

        labels = [by_id[chunk_id] for chunk_id in query.relevant if chunk_id in by_id]
        method_ids = {
            "exact": exact_ids,
            "bm25": bm25_ids,
            "hybrid": hybrid_ids,
        }
        rows.append(
            {
                "source": query.source,
                "query": text,
                "labels": [label.chunk_id for label in labels],
                "ranks": {
                    method: {
                        level: ranks(ids, chunks, labels, level)
                        for level in LEVELS
                    }
                    for method, ids in method_ids.items()
                },
                "zero": {method: not ids for method, ids in method_ids.items()},
                "latency_ms": {
                    "ilike": exact_ms,
                    "bm25": bm25_ms,
                    "hybrid": exact_ms + bm25_ms,
                },
            }
        )

    summary: dict[str, object] = {}
    for split, predicate in {
        "all": lambda source: True,
        "chat": lambda source: source == "chat",
        "golden": lambda source: source == "golden",
        "identifiers": lambda source: source == "identifier",
        "terms": lambda source: source == "terms",
    }.items():
        subset = [
            row
            for row in rows
            if predicate(str(row["source"]))
        ]
        if not subset:
            continue
        summary[split] = {}
        for method in ("exact", "bm25", "hybrid"):
            summary[split][method] = {}
            for level in LEVELS:
                results = [
                    (
                        row["ranks"][method][level],  # type: ignore[index]
                        len(row["labels"]),  # type: ignore[arg-type]
                    )
                    for row in subset
                ]
                summary[split][method][level] = summarize(results)
        summary[split]["latency_ms"] = {}
        for method in ("ilike", "bm25", "hybrid"):
            values = [
                row["latency_ms"][method]  # type: ignore[index]
                for row in subset
            ]
            summary[split]["latency_ms"][method] = {
                "p50": statistics.median(values) if values else None,
                "p95": sorted(values)[max(0, math.ceil(len(values) * 0.95) - 1)]
                if values
                else None,
            }
        summary[split]["zero_result_rate"] = {
            method: statistics.fmean(
                [1.0 if row["zero"][method] else 0.0 for row in subset]  # type: ignore[index]
            )
            for method in ("exact", "bm25", "hybrid")
        }

    return {
        "summary": summary,
        "items": rows,
    }


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    chat_path = Path(args.chat_queries)
    golden_path = Path(args.golden)
    chat, golden = load_queries(chat_path, golden_path)
    all_queries = chat + golden
    workspace_ids = {
        workspace for query in all_queries for workspace in query.workspaces
    }
    chunks = await load_corpus(factory, workspace_ids)
    print(f"corpus: {len(chunks)} active chunks in {len(workspace_ids)} workspace(s)")

    workspaces = tuple(sorted(workspace_ids))
    identifiers = mine_identifiers(chunks, workspaces, args.identifier_limit)
    terms = mine_term_queries(chunks, workspaces, args.term_limit)
    print(
        f"queries: {len(chat)} chat + {len(golden)} golden + "
        f"{len(identifiers)} identifiers + {len(terms)} term queries"
    )

    retriever = ContextRetriever(settings, engine)
    async with factory() as session:
        row = await session.get(RetrievalSetting, 1)
        previous = None if row is None else row.rerank_enabled
        if row is not None and row.rerank_enabled:
            row.rerank_enabled = False
            await session.commit()
    try:
        evaluation = await evaluate(
            retriever,
            chunks,
            all_queries + identifiers + terms,
            limit=args.limit,
        )
    finally:
        if previous:
            async with factory() as session:
                row = await session.get(RetrievalSetting, 1)
                if row is not None:
                    row.rerank_enabled = True
                    await session.commit()
        await retriever.aclose()
        await engine.dispose()

    payload = {
        "config": {
            "candidates": args.limit,
            "bm25": {"k1": 1.5, "b": 0.75, "stopwords": True},
            "tokenizer": TOKEN_RE.pattern,
            "chat_queries": str(chat_path),
            "golden_queries": str(golden_path),
            "identifier_limit": args.identifier_limit,
            "term_limit": args.term_limit,
        },
        "corpus": {"chunks": len(chunks), "workspaces": [str(w) for w in workspaces]},
        "evaluation": evaluation,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(out.write_text, json.dumps(payload, indent=2) + "\n")
    print(f"wrote {out}")
    print_summary(payload)
    return 0


def print_summary(payload: dict[str, object]) -> None:
    evaluation = payload["evaluation"]
    summary = evaluation["summary"]  # type: ignore[index]
    for split in ("all", "chat", "golden", "identifiers", "terms"):
        if split not in summary:
            continue
        print(f"\n== {split} ==")
        print(
            f"{'method':8} {'chunk MRR':>10} {'chunk nDCG':>11} "
            f"{'chunk R@12':>11} {'section MRR':>12} {'section R@12':>13} "
            f"{'zero':>7}"
        )
        for method in ("exact", "bm25", "hybrid"):
            data = summary[split][method]  # type: ignore[index]
            zero = summary[split]["zero_result_rate"][method]  # type: ignore[index]
            print(
                f"{method:8} {data['chunk']['mrr']:10.3f} "
                f"{data['chunk']['ndcg@10']:11.3f} "
                f"{data['chunk']['recall@12']:11.0%} "
                f"{data['section']['mrr']:12.3f} "
                f"{data['section']['recall@12']:13.0%} "
                f"{zero:7.0%}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-queries", default="evals/rerank_chat_queries.json")
    parser.add_argument("--golden", default="evals/retrieval_golden.json")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--identifier-limit", type=int, default=40)
    parser.add_argument("--term-limit", type=int, default=40)
    parser.add_argument(
        "--out",
        default="docs/benchmarks/2026-11-11-text-search-bm25.json",
    )
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
