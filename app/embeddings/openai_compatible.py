import math
from typing import Any

import httpx

from app.embeddings.base import EmbeddingProviderError


def truncate_normalize(vector: list[float], dimensions: int) -> list[float]:
    """Cut an embedding down to ``dimensions`` and make it unit length again.

    Matryoshka-trained models front-load their information, so keeping the
    first components and re-normalizing is the supported way to shrink a
    vector. Cosine similarity is scale-invariant, so the renormalization
    changes no ranking -- it just keeps the stored vectors unit-norm.
    """
    if len(vector) < dimensions:
        raise EmbeddingProviderError(
            f"Embedding response has {len(vector)} dimensions, "
            f"fewer than the configured {dimensions}",
            retryable=False,
        )
    if len(vector) == dimensions:
        return vector
    truncated = vector[:dimensions]
    norm = math.sqrt(math.fsum(value * value for value in truncated))
    if norm == 0:
        raise EmbeddingProviderError(
            "Embedding response truncated to a zero vector", retryable=False
        )
    return [value / norm for value in truncated]


class OpenAICompatibleEmbeddingProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._model = model
        self._dimensions = dimensions
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            transport=transport,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if any(not text.strip() for text in texts):
            raise EmbeddingProviderError("Embedding inputs must not be empty", retryable=False)
        try:
            response = await self._client.post(
                "embeddings",
                json={"model": self._model, "input": texts},
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise EmbeddingProviderError(
                "Embedding service request failed", retryable=True
            ) from exc

        if response.status_code >= 400:
            raise EmbeddingProviderError(
                f"Embedding service returned HTTP {response.status_code}",
                retryable=response.status_code in {408, 429} or response.status_code >= 500,
            )
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise EmbeddingProviderError(
                "Embedding service returned invalid JSON", retryable=False
            ) from exc
        return self._parse_embeddings(payload, expected_count=len(texts))

    def _parse_embeddings(self, payload: Any, *, expected_count: int) -> list[list[float]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise EmbeddingProviderError(
                "Embedding response is missing the data array", retryable=False
            )

        ordered: list[list[float] | None] = [None] * expected_count
        for item in payload["data"]:
            if not isinstance(item, dict):
                raise EmbeddingProviderError(
                    "Embedding response contains an invalid item", retryable=False
                )
            index = item.get("index")
            raw_vector = item.get("embedding")
            if type(index) is not int or not 0 <= index < expected_count:
                raise EmbeddingProviderError(
                    "Embedding response contains an invalid index", retryable=False
                )
            if ordered[index] is not None:
                raise EmbeddingProviderError(
                    "Embedding response contains a duplicate index", retryable=False
                )
            if not isinstance(raw_vector, list) or len(raw_vector) < self._dimensions:
                raise EmbeddingProviderError(
                    f"Embedding response must contain at least "
                    f"{self._dimensions} dimensions",
                    retryable=False,
                )
            vector: list[float] = []
            for value in raw_vector:
                if isinstance(value, bool) or not isinstance(value, int | float):
                    raise EmbeddingProviderError(
                        "Embedding response contains a non-numeric value",
                        retryable=False,
                    )
                number = float(value)
                if not math.isfinite(number):
                    raise EmbeddingProviderError(
                        "Embedding response contains a non-finite value",
                        retryable=False,
                    )
                vector.append(number)
            ordered[index] = vector

        if any(vector is None for vector in ordered):
            raise EmbeddingProviderError(
                "Embedding response count does not match the request", retryable=False
            )
        return [
            truncate_normalize(vector, self._dimensions)
            for vector in ordered
            if vector is not None
        ]

    async def aclose(self) -> None:
        await self._client.aclose()
