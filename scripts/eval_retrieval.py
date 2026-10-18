#!/usr/bin/env python
"""Measure retrieval quality against a golden set of questions.

The retrieval tests say the pipeline is well-formed; none of them say whether
a question actually finds the passage that answers it. This harness closes that
gap: it sends questions through the same ``ContextRetriever`` the MCP
``search_context`` tool uses, and reports recall, MRR, and how much the heading
neighbour expansion contributed.

Two steps, both against the live stack (PostgreSQL, Qdrant and the embedding
server must be running; LM Studio serves the chat model too):

    uv run python scripts/eval_retrieval.py generate --chat-model <name> \
        --count 25 --out evals/retrieval_golden.json

        Samples random chunks from the indexed corpus and asks the chat model
        for one question per chunk. The chunk the question was written from is
        the expected answer, so nothing needs hand labelling. The prompt
        forbids quoting the passage -- a question that reuses the chunk's
        phrases would be found by keyword overlap and would flatter whatever
        method is being measured -- but spot-check the file once.

    uv run python scripts/eval_retrieval.py run --queries evals/retrieval_golden.json

        Replays the golden set twice per question: once with expansion off, to
        see the raw similarity rank, and once as the MCP tool runs it. Use
        --limit/--min-score/--expand-width to A/B a setting without editing
        .env, and --json to keep a run for comparison after a change.

The script is read-only: it never writes to the database or the vector store.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import Settings  # noqa: E402
from app.retrieval.context import (  # noqa: E402
    ContextRetriever,
    RetrievalUnavailable,
)

DEFAULT_GOLDEN = Path("evals/retrieval_golden.json")

SAMPLE_CHUNKS = text("""
    SELECT chunk.id, chunk.workspace_id, chunk.document_id, chunk.text
    FROM document_chunks AS chunk
    JOIN documents AS document
      ON document.id = chunk.document_id
     AND document.workspace_id = chunk.workspace_id
    WHERE document.deleted_at IS NULL
      AND document.active_processing_run_id = chunk.processing_run_id
      AND chunk.token_count >= :min_tokens
    ORDER BY random()
    LIMIT :count
""")

QUESTION_PROMPT = (
    "Write one specific question that can be answered only from the passage "
    "below. Write it the way a person would ask: do not quote the passage, do "
    "not reuse its distinctive phrases, and do not mention 'the passage'. "
    "Return the question alone, with no preamble and no quotation marks.\n\n"
    "<passage>\n{passage}\n</passage>"
)


class EvalError(Exception):
    """Raised when the harness cannot produce a trustworthy measurement."""


@dataclass(frozen=True)
class QueryOutcome:
    query: str
    # 1-based position among similarity matches, None when the expected chunk
    # did not clear the threshold at all.
    rank: int | None
    # Whether the expected chunk reached the expanded output the model sees.
    in_final: bool
    matches: int
    final: int
    # Returned ids keep a miss auditable: without them, deciding whether the
    # expected chunk lost to a wrong match or to a legitimate duplicate answer
    # means digging through the database again.
    matched_ids: tuple[str, ...] = ()
    final_ids: tuple[str, ...] = ()
    error: str | None = None


async def generate(args: argparse.Namespace) -> int:
    settings = Settings()
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            if args.seed is not None:
                # PostgreSQL's random() is seeded per session, so the same seed
                # samples the same chunks for a repeatable golden set.
                await connection.execute(
                    text("SELECT setseed(:seed)"), {"seed": args.seed}
                )
            rows = (
                (
                    await connection.execute(
                        SAMPLE_CHUNKS,
                        {"min_tokens": args.min_tokens, "count": args.count},
                    )
                )
                .mappings()
                .all()
            )
    finally:
        await engine.dispose()

    if not rows:
        raise EvalError(
            "No indexed chunks were found. Ingest a document first, or lower "
            "--min-tokens."
        )

    queries: list[dict[str, str]] = []
    async with _model_client(settings) as client:
        for index, row in enumerate(rows, start=1):
            question = await _write_question(client, args.chat_model, str(row["text"]))
            print(f"[{index}/{len(rows)}] {question}")
            queries.append(
                {
                    "query": question,
                    "chunk_id": str(row["id"]),
                    "workspace_id": str(row["workspace_id"]),
                    "document_id": str(row["document_id"]),
                }
            )

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "chat_model": args.chat_model,
        "embedding_model": settings.embedding_model,
        "queries": queries,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(out.write_text, json.dumps(payload, indent=2) + "\n")
    print(f"\nWrote {len(queries)} questions to {out}")
    return 0


async def _write_question(
    client: httpx.AsyncClient, model: str, passage: str
) -> str:
    response = await client.post(
        "chat/completions",
        json={
            "model": model,
            "messages": [
                {"role": "user", "content": QUESTION_PROMPT.format(passage=passage)}
            ],
            "temperature": 0.2,
            "max_tokens": 120,
        },
    )
    response.raise_for_status()
    payload = response.json()
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise EvalError(f"The chat model returned an unusable response: {payload!r}") from exc
    question = str(content).strip().strip('"').strip()
    if not question:
        raise EvalError("The chat model returned an empty question")
    return question


async def run(args: argparse.Namespace) -> int:
    golden_path = Path(args.queries)
    if not await asyncio.to_thread(golden_path.exists):
        raise EvalError(
            f"{golden_path} does not exist. Run the generate step first."
        )
    golden = json.loads(await asyncio.to_thread(golden_path.read_text))
    queries = golden.get("queries", [])
    if not queries:
        raise EvalError(f"{golden_path} contains no queries")

    settings = Settings()
    overrides: dict[str, object] = {}
    if args.limit is not None:
        overrides["max_context_chunks"] = args.limit
    if args.min_score is not None:
        overrides["min_relevance_score"] = args.min_score
    if args.expand_width is not None:
        overrides["heading_expand_chunks"] = args.expand_width
    settings = settings.model_copy(update=overrides)
    # The raw pass disables expansion so a neighbour cannot stand in for a
    # similarity match that was missed.
    raw_settings = settings.model_copy(update={"heading_expand_chunks": 0})

    engine = create_async_engine(settings.database_url)
    expanded = ContextRetriever(settings, engine)
    raw = ContextRetriever(raw_settings, engine)
    outcomes: list[QueryOutcome] = []
    try:
        for item in queries:
            expected = _uuid_field(item, "chunk_id", golden_path)
            workspace = _uuid_field(item, "workspace_id", golden_path)
            try:
                matches = await raw.retrieve(
                    item["query"], [workspace], settings.max_context_chunks
                )
                sources = await expanded.retrieve(
                    item["query"], [workspace], settings.max_context_chunks
                )
            except RetrievalUnavailable as exc:
                outcomes.append(
                    QueryOutcome(
                        query=item["query"],
                        rank=None,
                        in_final=False,
                        matches=0,
                        final=0,
                        error=str(exc),
                    )
                )
                continue
            rank = next(
                (
                    position
                    for position, source in enumerate(matches, start=1)
                    if source.chunk_id == expected
                ),
                None,
            )
            outcomes.append(
                QueryOutcome(
                    query=item["query"],
                    rank=rank,
                    in_final=any(source.chunk_id == expected for source in sources),
                    matches=len(matches),
                    final=len(sources),
                    matched_ids=tuple(str(source.chunk_id) for source in matches),
                    final_ids=tuple(str(source.chunk_id) for source in sources),
                )
            )
    finally:
        await raw.aclose()
        await expanded.aclose()

    _report(outcomes, settings, args)
    return 0 if not any(outcome.error for outcome in outcomes) else 1


def _uuid_field(item: dict[str, str], field: str, path: Path) -> UUID:
    try:
        return UUID(str(item[field]))
    except (KeyError, ValueError) as exc:
        raise EvalError(
            f"{path} has a query without a valid {field}: {item.get('query')!r}"
        ) from exc


def _report(
    outcomes: list[QueryOutcome], settings: Settings, args: argparse.Namespace
) -> None:
    width = min(max(len(outcome.query) for outcome in outcomes), 60)
    print(
        f"\nlimit={settings.max_context_chunks} "
        f"min_score={settings.min_relevance_score} "
        f"expand_width={settings.heading_expand_chunks} "
        f"model={settings.embedding_model}\n"
    )
    for outcome in outcomes:
        rank = "  -" if outcome.rank is None else f"{outcome.rank:>3}"
        state = "error" if outcome.error else ("hit" if outcome.in_final else "miss")
        print(
            f"rank={rank}  {state:<5}  "
            f"matches={outcome.matches}  final={outcome.final}  "
            f"{outcome.query[:width]}"
        )

    total = len(outcomes)
    errors = [outcome for outcome in outcomes if outcome.error]
    ranked = [outcome for outcome in outcomes if outcome.rank is not None]
    summary = {
        "queries": total,
        "errors": len(errors),
        "recall@1": sum(1 for outcome in outcomes if outcome.rank == 1) / total,
        f"recall@{settings.max_context_chunks}": len(ranked) / total,
        "mrr": sum(1.0 / outcome.rank for outcome in ranked if outcome.rank) / total,
        "in_final_output": (
            sum(1 for outcome in outcomes if outcome.in_final) / total
        ),
        "expansion_only_hits": sum(
            1 for outcome in outcomes if outcome.rank is None and outcome.in_final
        ),
        "avg_matches": sum(outcome.matches for outcome in outcomes) / total,
        "avg_final": sum(outcome.final for outcome in outcomes) / total,
    }
    recall_k = summary[f"recall@{settings.max_context_chunks}"]
    print(
        f"\nrecall@1={summary['recall@1']:.0%}  "
        f"recall@{settings.max_context_chunks}={recall_k:.0%}  "
        f"mrr={summary['mrr']:.2f}  "
        f"in_final={summary['in_final_output']:.0%}  "
        f"expansion_only={summary['expansion_only_hits']}  "
        f"avg_matches={summary['avg_matches']:.1f}  "
        f"avg_final={summary['avg_final']:.1f}"
    )
    if errors:
        print(
            f"\n{len(errors)} of {total} queries failed. If every one failed, "
            "check that LM Studio, Qdrant and PostgreSQL are reachable."
        )
    if args.json_out:
        payload = {
            "ran_at": datetime.now(UTC).isoformat(),
            "config": {
                "embedding_model": settings.embedding_model,
                "embedding_profile": settings.embedding_profile,
                "max_context_chunks": settings.max_context_chunks,
                "min_relevance_score": settings.min_relevance_score,
                "heading_expand_chunks": settings.heading_expand_chunks,
                "heading_chunk_max_tokens": settings.heading_chunk_max_tokens,
            },
            "summary": summary,
            "queries": [asdict(outcome) for outcome in outcomes],
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2) + "\n")
        print(f"Wrote run detail to {args.json_out}")


def _model_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"{settings.embedding_base_url.rstrip('/')}/",
        headers={
            "Authorization": f"Bearer {settings.embedding_api_key.get_secret_value()}"
        },
        timeout=settings.embedding_timeout_seconds,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser(
        "generate", help="write questions from sampled chunks"
    )
    generate_parser.add_argument(
        "--chat-model", required=True, help="model id to ask for questions"
    )
    generate_parser.add_argument("--count", type=int, default=25)
    generate_parser.add_argument(
        "--min-tokens",
        type=int,
        default=30,
        help="skip chunks shorter than this; short chunks make degenerate questions",
    )
    generate_parser.add_argument(
        "--seed", type=float, default=None, help="sample the same chunks again"
    )
    generate_parser.add_argument("--out", default=str(DEFAULT_GOLDEN))

    run_parser = subparsers.add_parser(
        "run", help="replay a golden set through the retriever"
    )
    run_parser.add_argument("--queries", default=str(DEFAULT_GOLDEN))
    run_parser.add_argument("--limit", type=int, default=None)
    run_parser.add_argument("--min-score", type=float, default=None)
    run_parser.add_argument("--expand-width", type=int, default=None)
    run_parser.add_argument("--json", dest="json_out", default=None)

    args = parser.parse_args()
    try:
        if args.command == "generate":
            return asyncio.run(generate(args))
        return asyncio.run(run(args))
    except httpx.ConnectError:
        print(
            f"Could not reach the model server at {Settings().embedding_base_url}. "
            "Is LM Studio running?",
            file=sys.stderr,
        )
        return 2
    except EvalError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
