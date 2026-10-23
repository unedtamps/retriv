from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID


class EmbeddingProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class VectorStoreError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class VectorPoint:
    id: UUID
    vector: list[float]
    payload: dict[str, Any]


@dataclass(frozen=True)
class VectorMatch:
    id: UUID
    score: float
    payload: dict[str, Any]


class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    async def aclose(self) -> None: ...


class VectorStore(Protocol):
    async def ensure_collection(self) -> None: ...

    async def upsert(self, points: list[VectorPoint]) -> None: ...

    async def delete_points(self, point_ids: list[UUID]) -> None: ...

    async def delete_document(self, document_id: UUID) -> None: ...

    async def set_document_workspaces(
        self, document_id: UUID, workspace_ids: list[UUID]
    ) -> None: ...

    async def search(
        self,
        vector: list[float],
        *,
        workspace_ids: list[UUID],
        embedding_profile: str,
        limit: int,
    ) -> list[VectorMatch]: ...

    async def aclose(self) -> None: ...
