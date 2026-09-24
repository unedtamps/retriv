import json
from uuid import uuid4

import httpx
import pytest

from app.embeddings.base import EmbeddingProviderError, VectorPoint, VectorStoreError
from app.embeddings.openai_compatible import OpenAICompatibleEmbeddingProvider
from app.embeddings.qdrant import QdrantVectorStore


async def test_openai_provider_preserves_response_index_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.content) == {
            "model": "embedding-model",
            "input": ["first", "second"],
        }
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0, 0.0]},
                    {"index": 0, "embedding": [1.0, 0.0, 0.0]},
                ]
            },
        )

    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://embeddings.test/v1",
        api_key="test-key",
        model="embedding-model",
        dimensions=3,
        timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    try:
        vectors = await provider.embed(["first", "second"])
    finally:
        await provider.aclose()

    assert vectors == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]


async def test_openai_provider_rejects_wrong_dimensions() -> None:
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://embeddings.test/v1",
        api_key="test-key",
        model="embedding-model",
        dimensions=3,
        timeout_seconds=1,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]}
            )
        ),
    )
    try:
        with pytest.raises(EmbeddingProviderError, match="3 dimensions") as error:
            await provider.embed(["first"])
    finally:
        await provider.aclose()

    assert error.value.retryable is False


async def test_openai_provider_marks_server_errors_retryable() -> None:
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://embeddings.test/v1",
        api_key="test-key",
        model="embedding-model",
        dimensions=3,
        timeout_seconds=1,
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
    )
    try:
        with pytest.raises(EmbeddingProviderError) as error:
            await provider.embed(["first"])
    finally:
        await provider.aclose()

    assert error.value.retryable is True


async def test_qdrant_store_creates_collection_upserts_metadata_and_deletes() -> None:
    collection_exists = False
    requests: list[tuple[str, str, dict[str, object] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal collection_exists
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.method == "GET":
            if not collection_exists:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "result": {
                        "config": {
                            "params": {
                                "vectors": {"size": 4, "distance": "Cosine"}
                            }
                        }
                    }
                },
            )
        if request.method == "PUT" and request.url.path.endswith("/vectors-v1"):
            collection_exists = True
            return httpx.Response(200, json={"result": True})
        if request.url.path.endswith("/index"):
            return httpx.Response(200, json={"result": {"status": "completed"}})
        return httpx.Response(200, json={"result": {"status": "completed"}})

    store = QdrantVectorStore(
        url="http://qdrant.test",
        api_key="qdrant-key",
        collection="vectors-v1",
        dimensions=4,
        timeout_seconds=1,
        vectors_on_disk=True,
        transport=httpx.MockTransport(handler),
    )
    workspace_id = uuid4()
    document_id = uuid4()
    point = VectorPoint(
        id=uuid4(),
        vector=[1.0, 0.0, 0.0, 0.0],
        payload={
            "workspace_id": str(workspace_id),
            "document_id": str(document_id),
            "chunk_id": str(uuid4()),
        },
    )
    try:
        await store.upsert([point])
        await store.delete_document(workspace_id, document_id)
    finally:
        await store.aclose()

    create_request = next(
        body
        for method, path, body in requests
        if method == "PUT" and path.endswith("/vectors-v1")
    )
    assert create_request == {
        "vectors": {"size": 4, "distance": "Cosine", "on_disk": True},
        "hnsw_config": {"on_disk": True},
    }
    upsert_request = next(
        body
        for method, path, body in requests
        if method == "PUT" and path.endswith("/points")
    )
    assert upsert_request is not None
    assert upsert_request["points"][0]["payload"] == point.payload  # type: ignore[index]
    assert "text" not in upsert_request["points"][0]["payload"]  # type: ignore[index]
    delete_request = next(
        body
        for method, path, body in requests
        if method == "POST" and path.endswith("/points/delete")
    )
    assert delete_request == {
        "filter": {
            "must": [
                {"key": "workspace_id", "match": {"value": str(workspace_id)}},
                {"key": "document_id", "match": {"value": str(document_id)}},
            ]
        }
    }


async def test_qdrant_store_rejects_mismatched_collection_dimensions() -> None:
    store = QdrantVectorStore(
        url="http://qdrant.test",
        api_key="qdrant-key",
        collection="vectors-v1",
        dimensions=4,
        timeout_seconds=1,
        vectors_on_disk=True,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "result": {
                        "config": {
                            "params": {
                                "vectors": {"size": 8, "distance": "Cosine"}
                            }
                        }
                    }
                },
            )
        ),
    )
    try:
        with pytest.raises(VectorStoreError, match="does not match") as error:
            await store.ensure_collection()
    finally:
        await store.aclose()

    assert error.value.retryable is False


async def test_qdrant_search_mandates_workspace_and_profile_payload_filters() -> None:
    workspace_id = uuid4()
    point_id = uuid4()
    chunk_id = uuid4()
    query_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_body
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "config": {
                            "params": {
                                "vectors": {"size": 4, "distance": "Cosine"}
                            }
                        }
                    }
                },
            )
        if request.url.path.endswith("/index"):
            return httpx.Response(200, json={"result": {"status": "completed"}})
        query_body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "result": {
                    "points": [
                        {
                            "id": str(point_id),
                            "score": 0.91,
                            "payload": {
                                "workspace_id": str(workspace_id),
                                "chunk_id": str(chunk_id),
                                "embedding_profile": "test-profile",
                            },
                        }
                    ]
                }
            },
        )

    store = QdrantVectorStore(
        url="http://qdrant.test",
        api_key="qdrant-key",
        collection="vectors-v1",
        dimensions=4,
        timeout_seconds=1,
        vectors_on_disk=True,
        transport=httpx.MockTransport(handler),
    )
    try:
        matches = await store.search(
            [0.1, 0.2, 0.3, 0.4],
            workspace_ids=[workspace_id],
            embedding_profile="test-profile",
            limit=7,
        )
    finally:
        await store.aclose()

    assert len(matches) == 1
    assert matches[0].id == point_id
    assert matches[0].payload["chunk_id"] == str(chunk_id)
    assert query_body == {
        "query": [0.1, 0.2, 0.3, 0.4],
        "filter": {
            "must": [
                {
                    "key": "workspace_id",
                    "match": {"any": [str(workspace_id)]},
                },
                {
                    "key": "embedding_profile",
                    "match": {"value": "test-profile"},
                },
            ]
        },
        "limit": 7,
        "with_payload": [
            "workspace_id",
            "document_id",
            "processing_run_id",
            "chunk_id",
            "content_hash",
            "embedding_profile",
        ],
        "with_vector": False,
    }
