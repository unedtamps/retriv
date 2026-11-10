"""Optional second-stage reranking for retrieved passages.

The retriever's cosine order is the baseline and the reranker is strictly an
add-on: it is disabled unless an admin turns it on, and any failure in the
decision model falls back to the order cosine produced.

Laya is a typed-decision model rather than a text generator. Each candidate
is scored pointwise as ``irrelevant / partially relevant / directly answers``;
the expected score (0-2) is the ranking key. Batching several candidates into
one request was measured to be slower and less reliable than a small pool of
parallel pointwise calls, so the adapter keeps one passage per request and
bounds concurrency instead.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from app.retrieval.schemas import ContextSource

logger = logging.getLogger(__name__)

SCORE_CRITERIA = ["irrelevant", "partially relevant", "directly answers"]
MAX_CANDIDATE_CHARS = 1_200
DEFAULT_CONCURRENCY = 8


@dataclass(frozen=True)
class RerankConfig:
    model: str
    candidates: int
    concurrency: int
    min_score: float | None
    timeout_ms: int


@dataclass(frozen=True)
class RerankResult:
    sources: list[ContextSource]
    calls: int
    input_tokens: int
    elapsed_ms: int
    fallback: bool = False


def _candidate_state(query: str, source: ContextSource) -> str:
    location = ""
    if source.filename is not None:
        location = f" ({source.filename}"
        if source.page_numbers:
            location += f", page {source.page_numbers[0]}"
        location += ")"
    section = " / ".join(source.section_path)
    if section:
        location += f" [section: {section}]"
    return (
        f"Query: {query}\n\n"
        f"Candidate 1{location}:\n{source.text[:MAX_CANDIDATE_CHARS]}"
    )


class LayaReranker:
    """Score passages with the local Laya decision model.

    ``client`` is the embedding client: the same LM Studio instance serves
    Qwen embeddings and Laya decisions, so the base URL and bearer token are
    already on it. The relative ``systemone`` path keeps that assumption in
    one place.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        config: RerankConfig,
        *,
        concurrency: int | None = None,
    ) -> None:
        self._client = client
        self._config = config
        self._concurrency = max(1, concurrency or config.concurrency)

    async def rerank(
        self,
        query: str,
        sources: list[ContextSource],
        *,
        limit: int,
    ) -> RerankResult:
        if limit <= 0 or len(sources) <= 1:
            return RerankResult(
                sources=sources[:limit],
                calls=0,
                input_tokens=0,
                elapsed_ms=0,
            )

        started = time.perf_counter()
        candidates = sources[: self._config.candidates]
        semaphore = asyncio.Semaphore(self._concurrency)

        async def score(index: int, source: ContextSource) -> tuple[int, float, int]:
            async with semaphore:
                value, token_count = await self._score_one(query, source)
                return index, value, token_count

        try:
            outcomes = await asyncio.gather(
                *(score(index, source) for index, source in enumerate(candidates))
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "laya rerank failed after %d candidates in %d ms: %s: %s",
                len(candidates),
                elapsed,
                type(exc).__name__,
                exc,
            )
            return RerankResult(
                sources=sources[:limit],
                calls=len(candidates),
                input_tokens=0,
                elapsed_ms=elapsed,
                fallback=True,
            )

        elapsed = int((time.perf_counter() - started) * 1000)
        tokens = sum(token_count for _, _, token_count in outcomes)
        ordered = [
            source
            for index, score_value in sorted(
                ((index, score_value) for index, score_value, _ in outcomes),
                key=lambda item: (-item[1], item[0]),
            )
            for source in [candidates[index]]
            if self._config.min_score is None or score_value >= self._config.min_score
        ]
        logger.debug(
            "laya rerank: %d -> %d sources, %d calls, %d ms, %d tokens",
            len(candidates),
            len(ordered[:limit]),
            len(candidates),
            elapsed,
            tokens,
        )
        return RerankResult(
            sources=ordered[:limit],
            calls=len(candidates),
            input_tokens=tokens,
            elapsed_ms=elapsed,
        )

    async def _score_one(
        self,
        query: str,
        source: ContextSource,
    ) -> tuple[float, int]:
        response = await self._client.post(
            "systemone",
            json={
                "model": self._config.model,
                "state": _candidate_state(query, source),
                "questions": {
                    "c1": {
                        "type": "score",
                        "instructions": (
                            "Rate candidate 1 for how directly it answers the query."
                        ),
                        "criteria": SCORE_CRITERIA,
                    }
                },
            },
            timeout=self._config.timeout_ms / 1000,
        )
        response.raise_for_status()
        payload = response.json()
        answers = payload.get("answers")
        if not isinstance(answers, dict):
            raise ValueError("Laya response carried no answers object")
        answer = answers.get("c1")
        if not isinstance(answer, dict):
            raise ValueError("Laya response omitted the candidate")
        score = answer.get("score")
        if not isinstance(score, (int, float)):
            raise ValueError("Laya response carried no score")
        usage = payload.get("usage")
        token_count = usage.get("input_tokens", 0) if isinstance(usage, dict) else 0
        return (
            float(score),
            int(token_count) if isinstance(token_count, (int, float)) else 0,
        )
