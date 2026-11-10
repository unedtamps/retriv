"""The optional Laya reranker: ordering, threshold and fail-open behaviour."""

import json
from uuid import uuid4

import httpx
import pytest

from app.retrieval.rerank import LayaReranker, RerankConfig
from app.retrieval.schemas import ContextSource


def source(text: str) -> ContextSource:
    return ContextSource(
        workspace_id=uuid4(),
        document_id=uuid4(),
        processing_run_id=uuid4(),
        chunk_id=uuid4(),
        ordinal=1,
        score=0.5,
        content_hash="a" * 64,
        filename="report.pdf",
        section_path=["Revenue"],
        page_numbers=[2],
        text=text,
    )


def config(**overrides: object) -> RerankConfig:
    values: dict[str, object] = {
        "model": "laya",
        "candidates": 30,
        "concurrency": 2,
        "min_score": None,
        "timeout_ms": 1_000,
    }
    values.update(overrides)
    return RerankConfig(**values)  # type: ignore[arg-type]


def client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://laya.test/v1/", transport=handler)


@pytest.mark.asyncio
async def test_laya_scores_each_passage_and_orders_by_score() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        score = 2.0 if "good passage" in payload["state"] else 0.1
        return httpx.Response(
            200,
            json={
                "answers": {"c1": {"type": "score", "score": score}},
                "usage": {"input_tokens": 12},
            },
        )

    bad = source("bad passage")
    good = source("good passage")
    also_bad = source("another bad passage")
    async with client(httpx.MockTransport(handle)) as http:
        result = await LayaReranker(http, config()).rerank(
            "which passage is good?",
            [bad, good, also_bad],
            limit=2,
        )

    assert [entry.text for entry in result.sources] == ["good passage", "bad passage"]
    assert result.fallback is False
    assert result.calls == 3
    assert result.input_tokens == 36


@pytest.mark.asyncio
async def test_laya_threshold_drops_weak_candidates() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        score = 1.8 if "good" in payload["state"] else 0.2
        return httpx.Response(
            200,
            json={"answers": {"c1": {"score": score}}, "usage": {"input_tokens": 1}},
        )

    async with client(httpx.MockTransport(handle)) as http:
        result = await LayaReranker(http, config(min_score=1.0)).rerank(
            "query",
            [source("bad"), source("good")],
            limit=5,
        )

    assert [entry.text for entry in result.sources] == ["good"]


@pytest.mark.asyncio
async def test_laya_falls_back_to_cosine_order_on_failure() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, text="model unavailable")

    first = source("first")
    second = source("second")
    async with client(httpx.MockTransport(handle)) as http:
        result = await LayaReranker(http, config()).rerank(
            "query",
            [first, second],
            limit=2,
        )

    assert result.fallback is True
    assert result.sources == [first, second]
