#!/usr/bin/env python
"""Build a small Laya reranker training/evaluation set from the live corpus.

Positives are the chunks the assistant actually cited (chat questions) or the
chunk each generated question came from (golden questions). Negatives are the
top cosine candidates that are not in the positive set, so the head sees the
same hard distractors the retriever will hand it at inference.

The split is by question, never by passage: a question in eval has no rows in
train. The four real chat questions are always held out.

    uv run python scripts/build_rerank_training.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import Settings  # noqa: E402
from app.db.models import RetrievalSetting  # noqa: E402
from app.retrieval.context import ContextRetriever  # noqa: E402
from app.retrieval.schemas import ContextSource  # noqa: E402

CRITERIA = ["irrelevant", "partially relevant", "directly answers"]
MAX_CHARS = 1_200


def _state(query: str, source: ContextSource) -> str:
    location = ""
    if source.filename:
        location = f" ({source.filename}"
        if source.page_numbers:
            location += f", page {source.page_numbers[0]}"
        location += ")"
    return f"Query: {query}\n\nPassage{location}:\n{source.text[:MAX_CHARS]}"


def _case(query: str, source: ContextSource, gold: int) -> dict[str, object]:
    return {
        "state": _state(query, source),
        "questions": {
            "relevant": {
                "type": "score",
                "instructions": "Rate the passage for how directly it answers the query.",
                "criteria": CRITERIA,
            }
        },
        "expected": {"relevant": gold},
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

    chat = json.loads(
        await asyncio.to_thread(Path(args.chat_queries).read_text)
    )["queries"]
    golden = json.loads(await asyncio.to_thread(Path(args.golden).read_text))["queries"]

    questions: list[dict[str, object]] = []
    for entry in chat:
        relevant = {c["chunk_id"] for c in entry["citations"] if c.get("chunk_id")}
        workspaces = sorted(
            {UUID(c["workspace_id"]) for c in entry["citations"] if c.get("workspace_id")}
        )
        if relevant and workspaces:
            questions.append(
                {
                    "query": entry["query"],
                    "relevant": relevant,
                    "workspaces": workspaces,
                    "source": "chat",
                }
            )
    for entry in golden:
        if entry.get("chunk_id") and entry.get("workspace_id"):
            questions.append(
                {
                    "query": entry["query"],
                    "relevant": {entry["chunk_id"]},
                    "workspaces": [UUID(entry["workspace_id"])],
                    "source": "golden",
                }
            )

    rng = random.Random(args.seed)  # noqa: S311 - deterministic split, not cryptography
    golden_questions = [q for q in questions if q["source"] == "golden"]
    rng.shuffle(golden_questions)
    eval_golden_count = max(1, round(len(golden_questions) * args.eval_fraction))
    eval_questions = {
        id(q) for q in golden_questions[:eval_golden_count]
    } | {id(q) for q in questions if q["source"] == "chat"}

    rows: dict[str, list[dict[str, object]]] = {"train": [], "eval": []}
    for question in questions:
        split = "eval" if id(question) in eval_questions else "train"
        candidates = await retriever.retrieve(
            str(question["query"]),
            question["workspaces"],
            args.candidates,
            budget=args.candidates,
        )
        by_id = {str(source.chunk_id): source for source in candidates}
        for chunk_id in sorted(question["relevant"]):
            source = by_id.get(str(chunk_id))
            if source is not None:
                rows[split].append(_case(str(question["query"]), source, 2))
        negatives = [
            source for source in candidates if str(source.chunk_id) not in question["relevant"]
        ][: args.negatives]
        for source in negatives:
            rows[split].append(_case(str(question["query"]), source, 0))

    out = Path(args.out)
    await asyncio.to_thread(out.mkdir, parents=True, exist_ok=True)
    for split, items in rows.items():
        path = out / f"{split}.jsonl"
        await asyncio.to_thread(
            path.write_text, "\n".join(json.dumps(item) for item in items) + "\n"
        )
        print(f"{split}: {len(items)} cases -> {path}")
    eval_meta = [
        {
            "query": q["query"],
            "relevant": sorted(q["relevant"]),
            "workspaces": [str(w) for w in q["workspaces"]],
            "source": q["source"],
        }
        for q in questions
        if id(q) in eval_questions
    ]
    await asyncio.to_thread(
        (out / "eval_queries.json").write_text, json.dumps(eval_meta, indent=2) + "\n"
    )
    metadata = {
        "generated_at": datetime.now(UTC).isoformat(),
        "questions": len(questions),
        "train_questions": len(questions) - len(eval_questions),
        "eval_questions": len(eval_questions),
        "chat_questions": sum(1 for q in questions if q["source"] == "chat"),
        "golden_questions": len(golden_questions),
        "candidates": args.candidates,
        "negatives_per_question": args.negatives,
        "state_format": "Query + passage, score rubric 0/1/2",
    }
    await asyncio.to_thread(
        (out / "metadata.json").write_text, json.dumps(metadata, indent=2) + "\n"
    )
    print(json.dumps(metadata, indent=2))

    await retriever.aclose()
    await engine.dispose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-queries", default="evals/rerank_chat_queries.json")
    parser.add_argument("--golden", default="evals/retrieval_golden.json")
    parser.add_argument("--out", default="docs/reranker/training")
    parser.add_argument("--candidates", type=int, default=30)
    parser.add_argument("--negatives", type=int, default=5)
    parser.add_argument("--eval-fraction", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=17)
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
