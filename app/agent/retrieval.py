import hashlib
import math
from collections.abc import Sequence
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import JSON, Uuid, bindparam, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.agent.schemas import ContextSource
from app.core.config import Settings

PAYLOAD_FIELDS = (
    "workspace_id",
    "document_id",
    "processing_run_id",
    "chunk_id",
    "content_hash",
    "embedding_profile",
)

VALIDATE_CHUNKS = (
    text("""
    SELECT
        chunk.id AS chunk_id,
        chunk.workspace_id,
        chunk.document_id,
        chunk.processing_run_id,
        chunk.ordinal,
        chunk.text,
        chunk.content_hash,
        chunk.section_path,
        chunk.page_numbers,
        document.original_filename AS filename,
        embedding.qdrant_point_id
    FROM document_chunks AS chunk
    JOIN chunk_embeddings AS embedding
      ON embedding.chunk_id = chunk.id
     AND embedding.workspace_id = chunk.workspace_id
     AND embedding.document_id = chunk.document_id
     AND embedding.processing_run_id = chunk.processing_run_id
    JOIN documents AS document
      ON document.workspace_id = chunk.workspace_id
     AND document.id = chunk.document_id
    WHERE chunk.id IN :chunk_ids
      AND chunk.workspace_id IN :workspace_ids
      AND document.deleted_at IS NULL
      AND document.active_processing_run_id = chunk.processing_run_id
      AND embedding.embedding_profile = :embedding_profile
      AND embedding.model = :embedding_model
      AND embedding.dimensions = :embedding_dimensions
      AND embedding.status = 'indexed'
      AND embedding.content_hash = chunk.content_hash
    """)
    .bindparams(
        bindparam("chunk_ids", type_=Uuid(as_uuid=True), expanding=True),
        bindparam("workspace_ids", type_=Uuid(as_uuid=True), expanding=True),
    )
    .columns(
        chunk_id=Uuid(as_uuid=True),
        workspace_id=Uuid(as_uuid=True),
        document_id=Uuid(as_uuid=True),
        processing_run_id=Uuid(as_uuid=True),
        section_path=JSON(),
        page_numbers=JSON(),
        qdrant_point_id=Uuid(as_uuid=True),
    )
)


class RetrievalUnavailable(RuntimeError):
    """Raised when embedding, vector search, or row validation cannot complete."""


class OutOfScopeSearchResult(RuntimeError):
    """Raised when Qdrant returns a result outside the signed workspace scope."""


class ContextRetriever:
    def __init__(
        self,
        settings: Settings,
        engine: AsyncEngine,
        *,
        embedding_client: httpx.AsyncClient | None = None,
        qdrant_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._embedding_client = embedding_client or httpx.AsyncClient(
            base_url=f"{settings.embedding_base_url.rstrip('/')}/",
            headers={
                "Authorization": f"Bearer {settings.embedding_api_key.get_secret_value()}"
            },
            timeout=settings.embedding_timeout_seconds,
        )
        # Read-only key: this process writes vectors only from the worker,
        # so the answer path never needs write access to the collection.
        self._qdrant_client = qdrant_client or httpx.AsyncClient(
            base_url=f"{settings.qdrant_url.rstrip('/')}/",
            headers={
                "api-key": settings.qdrant_read_only_api_key.get_secret_value()
            },
            timeout=settings.qdrant_timeout_seconds,
        )

    async def retrieve(
        self, query: str, workspace_ids: list[UUID], limit: int
    ) -> list[ContextSource]:
        if not workspace_ids:
            raise RetrievalUnavailable(
                "A verified non-empty workspace scope is required"
            )
        if len(workspace_ids) != len(set(workspace_ids)):
            raise RetrievalUnavailable("The workspace scope contains duplicate IDs")

        try:
            vector = await self._embed_query(query)
            matches = await self._query_qdrant(vector, workspace_ids, limit)
            return await self._load_validated_chunks(matches, workspace_ids)
        except OutOfScopeSearchResult:
            raise
        except (
            httpx.HTTPError,
            SQLAlchemyError,
            IndexError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise RetrievalUnavailable(
                "Scoped context retrieval failed validation"
            ) from exc

    async def _embed_query(self, query: str) -> list[float]:
        response = await self._embedding_client.post(
            "embeddings",
            json={"model": self._settings.embedding_model, "input": [query]},
        )
        response.raise_for_status()
        payload: Any = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("Embedding response is missing its data array")
        data = payload["data"]
        if len(data) != 1 or not isinstance(data[0], dict):
            raise ValueError("Embedding response must contain exactly one vector")
        if data[0].get("index") != 0:
            raise ValueError("Embedding response has an invalid vector index")
        raw_vector = data[0].get("embedding")
        if (
            not isinstance(raw_vector, list)
            or len(raw_vector) != self._settings.embedding_dimensions
        ):
            raise ValueError(
                "Embedding response dimensions do not match the configured profile"
            )
        vector: list[float] = []
        for value in raw_vector:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(
                    "Embedding response contains a non-numeric vector value"
                )
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(
                    "Embedding response contains a non-finite vector value"
                )
            vector.append(number)
        return vector

    async def _query_qdrant(
        self, vector: list[float], workspace_ids: list[UUID], limit: int
    ) -> list[dict[str, Any]]:
        response = await self._qdrant_client.post(
            f"collections/{self._settings.qdrant_collection}/points/query",
            json={
                "query": vector,
                "filter": {
                    "must": [
                        {
                            "key": "workspace_id",
                            "match": {
                                "any": [
                                    str(workspace_id) for workspace_id in workspace_ids
                                ]
                            },
                        },
                        {
                            "key": "embedding_profile",
                            "match": {"value": self._settings.embedding_profile},
                        },
                    ]
                },
                "limit": limit,
                "with_payload": list(PAYLOAD_FIELDS),
                "with_vector": False,
            },
        )
        response.raise_for_status()
        payload: Any = response.json()
        matches = payload["result"]["points"]
        if not isinstance(matches, list):
            raise ValueError("Qdrant response points must be a list")
        if len(matches) > limit:
            raise ValueError("Qdrant returned more points than requested")

        allowed_workspace_ids = {str(workspace_id) for workspace_id in workspace_ids}
        seen_chunk_ids: set[UUID] = set()
        relevant_matches: list[dict[str, Any]] = []
        for match in matches:
            if not isinstance(match, dict):
                raise ValueError("Qdrant returned a malformed point")
            point_id = UUID(str(match["id"]))
            score = float(match["score"])
            point_payload = match["payload"]
            if not math.isfinite(score) or not isinstance(point_payload, dict):
                raise ValueError("Qdrant returned invalid score or payload metadata")
            workspace_id = point_payload.get("workspace_id")
            if workspace_id not in allowed_workspace_ids:
                raise OutOfScopeSearchResult(
                    f"Qdrant returned point {point_id} outside the signed workspace scope"
                )
            for field in PAYLOAD_FIELDS:
                if field not in point_payload:
                    raise ValueError(
                        f"Qdrant point is missing required {field} metadata"
                    )
            chunk_id = UUID(str(point_payload["chunk_id"]))
            if chunk_id in seen_chunk_ids:
                raise ValueError("Qdrant returned a duplicate chunk")
            seen_chunk_ids.add(chunk_id)
            if score >= self._settings.min_relevance_score:
                relevant_matches.append(match)
        return relevant_matches

    async def _load_validated_chunks(
        self, matches: Sequence[dict[str, Any]], workspace_ids: list[UUID]
    ) -> list[ContextSource]:
        if not matches:
            return []
        candidate_ids = [UUID(str(match["payload"]["chunk_id"])) for match in matches]
        async with self._sessions() as session:
            result = await session.execute(
                VALIDATE_CHUNKS,
                {
                    "chunk_ids": candidate_ids,
                    "workspace_ids": workspace_ids,
                    "embedding_profile": self._settings.embedding_profile,
                    "embedding_model": self._settings.embedding_model,
                    "embedding_dimensions": self._settings.embedding_dimensions,
                },
            )
            rows = {UUID(str(row["chunk_id"])): row for row in result.mappings()}

        sources: list[ContextSource] = []
        for match in matches:
            point_payload = match["payload"]
            chunk_id = UUID(str(point_payload["chunk_id"]))
            row = rows.get(chunk_id)
            if row is None:
                raise RetrievalUnavailable(
                    "A Qdrant point has no matching active, indexed PostgreSQL chunk"
                )
            point_id = UUID(str(match["id"]))
            expected_metadata = {
                "workspace_id": str(UUID(str(row["workspace_id"]))),
                "document_id": str(UUID(str(row["document_id"]))),
                "processing_run_id": str(UUID(str(row["processing_run_id"]))),
                "chunk_id": str(UUID(str(row["chunk_id"]))),
                "content_hash": row["content_hash"],
                "embedding_profile": self._settings.embedding_profile,
            }
            if any(
                point_payload.get(key) != value
                for key, value in expected_metadata.items()
            ):
                raise RetrievalUnavailable(
                    "Qdrant point metadata does not match PostgreSQL"
                )
            if UUID(str(row["qdrant_point_id"])) != point_id:
                raise RetrievalUnavailable(
                    "Qdrant point ID does not match its embedding ledger row"
                )
            text_hash = hashlib.sha256(str(row["text"]).encode()).hexdigest()
            if text_hash != row["content_hash"]:
                raise RetrievalUnavailable(
                    "PostgreSQL chunk text does not match its content hash"
                )
            sources.append(
                ContextSource(
                    workspace_id=UUID(str(row["workspace_id"])),
                    document_id=UUID(str(row["document_id"])),
                    processing_run_id=UUID(str(row["processing_run_id"])),
                    chunk_id=UUID(str(row["chunk_id"])),
                    ordinal=int(row["ordinal"]),
                    score=float(match["score"]),
                    content_hash=str(row["content_hash"]),
                    filename=row["filename"],
                    section_path=list(row["section_path"] or []),
                    page_numbers=list(row["page_numbers"] or []),
                    text=str(row["text"]),
                )
            )
        return sources

    async def aclose(self) -> None:
        await self._embedding_client.aclose()
        await self._qdrant_client.aclose()
        await self._engine.dispose()
