"""Optional second-stage reranking for retrieved passages.

The retriever's cosine order is the baseline and the reranker is strictly an
add-on: it is disabled unless an admin turns it on, and any failure in the
reranker falls back to the order cosine produced.

Two adapters share the same contract:

- ``LayaReranker`` scores each candidate pointwise as
  ``irrelevant / partially relevant / directly answers`` and sorts by the
  expected score (0-2). It is a typed-decision model rather than a text
  generator, so one passage per request with bounded concurrency measured
  faster and more reliably than batching.
- ``BgeReranker`` sends the whole candidate pool to a local llama.cpp
  cross-encoder and receives one relevance score per passage. Pure BGE
  reranking improved the first relevant rank but demoted answers below the
  retrieval window on the golden split, so its score is blended with cosine;
  ``rerank_blend`` is the BGE weight (0.3 was the best measured value).
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
    provider: str = "laya"
    blend: float = 1.0


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


def _normalize(values: list[float]) -> list[float]:
    """Min-max scale one query's scores into [0, 1].

    Cosine and cross-encoder scores live on different scales (cosine is
    bounded near 1, BGE logits are unbounded), so blending them needs a
    per-query scale. A constant list carries no ranking signal and becomes
    neutral instead of dividing by zero.
    """
    low, high = min(values), max(values)
    if high == low:
        return [0.5] * len(values)
    return [(value - low) / (high - low) for value in values]


class BgeReranker:
    """Score and blend passages with a local llama.cpp cross-encoder.

    The server exposes an OpenAI-shaped ``/v1/rerank`` endpoint when it is
    started with ``--reranking``. One request carries the whole pool; the
    response's ``relevance_score`` is a raw logit, so ``min_score`` (a Laya
    rubric value) does not apply and is ignored.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        config: RerankConfig,
    ) -> None:
        self._client = client
        self._config = config

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
        try:
            scores, tokens = await self._score_all(query, candidates)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            elapsed = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "bge rerank failed after %d candidates in %d ms: %s: %s",
                len(candidates),
                elapsed,
                type(exc).__name__,
                exc,
            )
            return RerankResult(
                sources=sources[:limit],
                calls=1,
                input_tokens=0,
                elapsed_ms=elapsed,
                fallback=True,
            )

        elapsed = int((time.perf_counter() - started) * 1000)
        weight = min(1.0, max(0.0, self._config.blend))
        if weight >= 1.0:
            keyed = [(score, index) for index, score in enumerate(scores)]
        else:
            cosine = _normalize([source.score for source in candidates])
            rerank = _normalize(scores)
            keyed = [
                ((1.0 - weight) * cosine_score + weight * rerank_score, index)
                for index, (cosine_score, rerank_score) in enumerate(
                    zip(cosine, rerank, strict=True)
                )
            ]
        ordered = [
            candidates[index]
            for _, index in sorted(keyed, key=lambda item: (-item[0], item[1]))
        ]
        logger.debug(
            "bge rerank: %d -> %d sources, blend %.2f, %d ms, %d tokens",
            len(candidates),
            len(ordered[:limit]),
            weight,
            elapsed,
            tokens,
        )
        return RerankResult(
            sources=ordered[:limit],
            calls=1,
            input_tokens=tokens,
            elapsed_ms=elapsed,
        )

    async def _score_all(
        self,
        query: str,
        candidates: list[ContextSource],
    ) -> tuple[list[float], int]:
        response = await self._client.post(
            "v1/rerank",
            json={
                "model": self._config.model,
                "query": query,
                "documents": [
                    source.text[:MAX_CANDIDATE_CHARS] for source in candidates
                ],
            },
            timeout=self._config.timeout_ms / 1000,
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results")
        if not isinstance(results, list) or len(results) != len(candidates):
            raise ValueError("BGE response did not score every candidate")

        scores = [0.0] * len(candidates)
        seen: set[int] = set()
        for entry in results:
            if not isinstance(entry, dict):
                raise ValueError("BGE response carried a malformed result")
            index = entry.get("index")
            value = entry.get("relevance_score")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index < 0
                or index >= len(candidates)
                or index in seen
            ):
                raise ValueError("BGE response carried an invalid candidate index")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError("BGE response carried no relevance score")
            scores[index] = float(value)
            seen.add(index)
        if len(seen) != len(candidates):
            raise ValueError("BGE response omitted a candidate")

        usage = payload.get("usage")
        token_count = 0
        if isinstance(usage, dict):
            token_count = usage.get("prompt_tokens") or usage.get("total_tokens") or 0
        return scores, int(token_count) if isinstance(token_count, (int, float)) else 0
