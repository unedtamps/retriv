"""The optional Laya reranker: ordering, threshold and fail-open behaviour."""

import json
from uuid import uuid4

import httpx
import pytest

from app.retrieval.context import ContextRetriever
from app.retrieval.rerank import BgeReranker, LayaReranker, RerankConfig
from app.retrieval.schemas import ContextSource


def source(text: str, score: float = 0.5) -> ContextSource:
    return ContextSource(
        workspace_id=uuid4(),
        document_id=uuid4(),
        processing_run_id=uuid4(),
        chunk_id=uuid4(),
        ordinal=1,
        score=score,
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


def bge_client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://bge.test/", transport=handler)


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


@pytest.mark.asyncio
async def test_bge_scores_the_pool_and_orders_by_relevance() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/rerank"
        payload = json.loads(request.content)
        assert payload["model"] == "bge-reranker-v2-m3"
        assert len(payload["documents"]) == 3
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 2, "relevance_score": -1.0},
                    {"index": 0, "relevance_score": 5.0},
                    {"index": 1, "relevance_score": 9.0},
                ],
                "usage": {"prompt_tokens": 42},
            },
        )

    first = source("first")
    second = source("second")
    third = source("third")
    async with bge_client(httpx.MockTransport(handle)) as http:
        result = await BgeReranker(
            http, config(model="bge-reranker-v2-m3", blend=1.0)
        ).rerank("query", [first, second, third], limit=3)

    assert [entry.text for entry in result.sources] == ["second", "first", "third"]
    assert result.fallback is False
    assert result.calls == 1
    assert result.input_tokens == 42


@pytest.mark.asyncio
async def test_bge_blend_balances_cosine_and_reranker_scores() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.0},
                    {"index": 1, "relevance_score": 10.0},
                    {"index": 2, "relevance_score": 5.0},
                ]
            },
        )

    first = source("first", score=0.9)
    second = source("second", score=0.8)
    third = source("third", score=0.1)
    async with bge_client(httpx.MockTransport(handle)) as http:
        result = await BgeReranker(
            http, config(model="bge", blend=0.3)
        ).rerank("query", [first, second, third], limit=2)

    # cosine: [1.0, 0.875, 0.0]; bge: [0.0, 1.0, 0.5]
    # blend:  [0.70, 0.9125, 0.15]
    assert [entry.text for entry in result.sources] == ["second", "first"]


@pytest.mark.asyncio
async def test_bge_blend_zero_keeps_cosine_order() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": -10.0},
                    {"index": 1, "relevance_score": 10.0},
                ]
            },
        )

    first = source("first", score=0.9)
    second = source("second", score=0.1)
    async with bge_client(httpx.MockTransport(handle)) as http:
        result = await BgeReranker(
            http, config(model="bge", blend=0.0)
        ).rerank("query", [first, second], limit=2)

    assert [entry.text for entry in result.sources] == ["first", "second"]


@pytest.mark.asyncio
async def test_bge_falls_back_when_a_candidate_is_missing() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={"results": [{"index": 0, "relevance_score": 1.0}]},
        )

    first = source("first")
    second = source("second")
    async with bge_client(httpx.MockTransport(handle)) as http:
        result = await BgeReranker(
            http, config(model="bge", blend=1.0)
        ).rerank("query", [first, second], limit=2)

    assert result.fallback is True
    assert result.sources == [first, second]


@pytest.mark.asyncio
async def test_bge_falls_back_to_cosine_order_on_failure() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, text="model unavailable")

    first = source("first")
    second = source("second")
    async with bge_client(httpx.MockTransport(handle)) as http:
        result = await BgeReranker(
            http, config(model="bge", blend=0.3)
        ).rerank("query", [first, second], limit=2)

    assert result.fallback is True
    assert result.sources == [first, second]


@pytest.mark.asyncio
async def test_context_dispatches_the_bge_provider_to_the_rerank_client() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/rerank"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": -5.0},
                    {"index": 1, "relevance_score": 5.0},
                ]
            },
        )

    def embedding_should_not_run(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected embedding call: {request.url}")

    first = source("first")
    second = source("second")
    retriever = object.__new__(ContextRetriever)
    retriever._rerank_client = bge_client(httpx.MockTransport(handle))
    retriever._embedding_client = httpx.AsyncClient(
        base_url="http://embedding.test/v1/",
        transport=httpx.MockTransport(embedding_should_not_run),
    )
    try:
        result = await retriever._rerank(
            "query",
            [first, second],
            limit=1,
            config=config(provider="bge", blend=1.0),
        )
    finally:
        await retriever._rerank_client.aclose()
        await retriever._embedding_client.aclose()

    assert [entry.text for entry in result] == ["second"]
