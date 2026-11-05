import json

import httpx
import pytest

from app.embeddings.base import EmbeddingProviderError
from app.embeddings.openai_compatible import OpenAICompatibleEmbeddingProvider, truncate_normalize


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


async def test_openai_provider_rejects_fewer_dimensions_than_configured() -> None:
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://embeddings.test/v1",
        api_key="test-key",
        model="embedding-model",
        dimensions=3,
        timeout_seconds=1,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})
        ),
    )
    try:
        with pytest.raises(EmbeddingProviderError, match="at least 3 dimensions") as error:
            await provider.embed(["first"])
    finally:
        await provider.aclose()

    assert error.value.retryable is False


async def test_openai_provider_truncates_and_renormalizes_wider_vectors() -> None:
    """The endpoint's native width may exceed the configured profile.

    The qwen3 embeddings are Matryoshka-trained: the first components carry
    the information, so truncating to the configured count and re-normalizing
    is the supported way to shrink them. A wider response than the profile
    is therefore normal, not an error.
    """
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://embeddings.test/v1",
        api_key="test-key",
        model="embedding-model",
        dimensions=2,
        timeout_seconds=1,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [3.0, 4.0, 99.0, 99.0]}]},
            )
        ),
    )
    try:
        vectors = await provider.embed(["first"])
    finally:
        await provider.aclose()

    assert vectors == [[0.6, 0.8]]


def test_truncate_normalize_rejects_a_shorter_vector() -> None:
    with pytest.raises(EmbeddingProviderError, match="fewer than"):
        truncate_normalize([1.0, 0.0], 3)


def test_truncate_normalize_rejects_a_truncated_zero_vector() -> None:
    with pytest.raises(EmbeddingProviderError, match="zero vector"):
        truncate_normalize([0.0, 0.0, 5.0], 2)


def test_truncate_normalize_passes_an_exact_width_vector_through() -> None:
    vector = [3.0, 4.0]
    assert truncate_normalize(vector, 2) is vector
