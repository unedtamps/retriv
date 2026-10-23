import asyncio
import math
from typing import Any
from uuid import UUID

import httpx

from app.embeddings.base import VectorMatch, VectorPoint, VectorStoreError


class QdrantVectorStore:
    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        collection: str,
        dimensions: int,
        timeout_seconds: float,
        vectors_on_disk: bool,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._collection = collection
        self._dimensions = dimensions
        self._vectors_on_disk = vectors_on_disk
        self._client = httpx.AsyncClient(
            base_url=f"{url.rstrip('/')}/",
            headers={"api-key": api_key},
            timeout=timeout_seconds,
            transport=transport,
        )
        self._ready = False
        self._ready_lock = asyncio.Lock()

    async def ensure_collection(self) -> None:
        if self._ready:
            return
        async with self._ready_lock:
            if self._ready:
                return
            response = await self._request(
                "GET",
                f"collections/{self._collection}",
                allowed_statuses={200, 404},
            )
            if response.status_code == 404:
                response = await self._request(
                    "PUT",
                    f"collections/{self._collection}",
                    json={
                        "vectors": {
                            "size": self._dimensions,
                            "distance": "Cosine",
                            "on_disk": self._vectors_on_disk,
                        },
                        "hnsw_config": {"on_disk": self._vectors_on_disk},
                    },
                    allowed_statuses={200, 409},
                )
                if response.status_code == 409:
                    response = await self._request(
                        "GET", f"collections/{self._collection}"
                    )
                else:
                    response = await self._request(
                        "GET", f"collections/{self._collection}"
                    )
            self._validate_collection(response)
            for field_name in (
                "workspace_ids",
                "origin_workspace_id",
                "document_id",
                "processing_run_id",
                "chunk_id",
                "embedding_profile",
                "content_hash",
            ):
                await self._request(
                    "PUT",
                    f"collections/{self._collection}/index",
                    params={"wait": "true"},
                    json={"field_name": field_name, "field_schema": "keyword"},
                )
            self._ready = True

    async def upsert(self, points: list[VectorPoint]) -> None:
        if not points:
            return
        for point in points:
            if len(point.vector) != self._dimensions:
                raise VectorStoreError(
                    f"Vector must contain {self._dimensions} dimensions",
                    retryable=False,
                )
            if any(not math.isfinite(value) for value in point.vector):
                raise VectorStoreError(
                    "Vector contains a non-finite value", retryable=False
                )
        await self.ensure_collection()
        response = await self._request(
            "PUT",
            f"collections/{self._collection}/points",
            params={"wait": "true"},
            json={
                "points": [
                    {
                        "id": str(point.id),
                        "vector": point.vector,
                        "payload": point.payload,
                    }
                    for point in points
                ]
            },
        )
        self._validate_completed_update(response)

    async def delete_points(self, point_ids: list[UUID]) -> None:
        if not point_ids:
            return
        await self.ensure_collection()
        response = await self._request(
            "POST",
            f"collections/{self._collection}/points/delete",
            params={"wait": "true"},
            json={"points": [str(point_id) for point_id in point_ids]},
        )
        self._validate_completed_update(response)

    async def delete_document(self, document_id: UUID) -> None:
        await self.ensure_collection()
        response = await self._request(
            "POST",
            f"collections/{self._collection}/points/delete",
            params={"wait": "true"},
            json={
                "filter": {
                    "must": [
                        {
                            "key": "document_id",
                            "match": {"value": str(document_id)},
                        },
                    ]
                }
            },
        )
        self._validate_completed_update(response)

    async def set_document_workspaces(
        self, document_id: UUID, workspace_ids: list[UUID]
    ) -> None:
        await self.ensure_collection()
        response = await self._request(
            "POST",
            f"collections/{self._collection}/points/payload",
            params={"wait": "true"},
            json={
                "payload": {
                    "workspace_ids": sorted(
                        {str(workspace_id) for workspace_id in workspace_ids}
                    )
                },
                "filter": {
                    "must": [
                        {
                            "key": "document_id",
                            "match": {"value": str(document_id)},
                        },
                    ]
                },
            },
        )
        self._validate_completed_update(response)

    async def search(
        self,
        vector: list[float],
        *,
        workspace_ids: list[UUID],
        embedding_profile: str,
        limit: int,
    ) -> list[VectorMatch]:
        if not workspace_ids:
            return []
        if len(vector) != self._dimensions or any(
            not math.isfinite(value) for value in vector
        ):
            raise VectorStoreError("Query vector has an invalid shape", retryable=False)
        await self.ensure_collection()
        response = await self._request(
            "POST",
            f"collections/{self._collection}/points/query",
            json={
                "query": vector,
                "filter": {
                    "must": [
                        {
                            "key": "workspace_ids",
                            "match": {"any": [str(item) for item in workspace_ids]},
                        },
                        {
                            "key": "embedding_profile",
                            "match": {"value": embedding_profile},
                        },
                    ]
                },
                "limit": limit,
                "with_payload": [
                    "origin_workspace_id",
                    "workspace_ids",
                    "document_id",
                    "processing_run_id",
                    "chunk_id",
                    "content_hash",
                    "embedding_profile",
                ],
                "with_vector": False,
            },
        )
        try:
            items = response.json()["result"]["points"]
            if not isinstance(items, list):
                raise TypeError("points is not a list")
            matches: list[VectorMatch] = []
            for item in items:
                point_id = UUID(str(item["id"]))
                score = float(item["score"])
                payload = item["payload"]
                if not isinstance(payload, dict) or not math.isfinite(score):
                    raise TypeError("invalid point payload or score")
                matches.append(VectorMatch(id=point_id, score=score, payload=payload))
            return matches
        except (KeyError, TypeError, ValueError) as exc:
            raise VectorStoreError(
                "Qdrant returned invalid search results", retryable=False
            ) from exc

    async def _request(
        self,
        method: str,
        path: str,
        *,
        allowed_statuses: set[int] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise VectorStoreError("Qdrant request failed", retryable=True) from exc
        accepted = allowed_statuses or {200}
        if response.status_code not in accepted:
            raise VectorStoreError(
                f"Qdrant returned HTTP {response.status_code}",
                retryable=response.status_code in {408, 429}
                or response.status_code >= 500,
            )
        return response

    def _validate_collection(self, response: httpx.Response) -> None:
        try:
            payload: Any = response.json()
            vectors = payload["result"]["config"]["params"]["vectors"]
            size = vectors["size"]
            distance = vectors["distance"]
        except (KeyError, TypeError, ValueError) as exc:
            raise VectorStoreError(
                "Qdrant returned invalid collection metadata", retryable=False
            ) from exc
        if size != self._dimensions or str(distance).lower() != "cosine":
            raise VectorStoreError(
                "Qdrant collection vector configuration does not match the embedding profile",
                retryable=False,
            )

    @staticmethod
    def _validate_completed_update(response: httpx.Response) -> None:
        try:
            status = response.json()["result"]["status"]
        except (KeyError, TypeError, ValueError) as exc:
            raise VectorStoreError(
                "Qdrant returned an invalid update response", retryable=False
            ) from exc
        if status != "completed":
            raise VectorStoreError(
                "Qdrant did not complete the requested update", retryable=True
            )

    async def aclose(self) -> None:
        await self._client.aclose()
