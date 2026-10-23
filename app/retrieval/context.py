import hashlib
import math
from collections.abc import Sequence
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import (
    JSON,
    Boolean,
    String,
    Uuid,
    and_,
    bindparam,
    case,
    exists,
    or_,
    select,
    text,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.config import Settings
from app.db.models import (
    EMBEDDING_INDEXED,
    ChunkEmbedding,
    Document,
    DocumentChunk,
    DocumentWorkspace,
)
from app.retrieval.schemas import ContextSource
from app.sections import LIKE_ESCAPE, section_descends_from, subtree_pattern

PAYLOAD_FIELDS = (
    "origin_workspace_id",
    "workspace_ids",
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
        (
            SELECT link.workspace_id
            FROM document_workspaces AS link
            WHERE link.document_id = chunk.document_id
              AND link.workspace_id IN :workspace_ids
              AND link.deleted_at IS NULL
            ORDER BY link.workspace_id
            LIMIT 1
        ) AS workspace_id,
        chunk.origin_workspace_id AS origin_workspace_id,
        chunk.document_id,
        chunk.processing_run_id,
        chunk.ordinal,
        chunk.text,
        chunk.token_count,
        chunk.content_hash,
        chunk.section_path,
        chunk.section_key,
        chunk.is_heading_only,
        chunk.page_numbers,
        (
            SELECT link.filename
            FROM document_workspaces AS link
            WHERE link.document_id = chunk.document_id
              AND link.workspace_id IN :workspace_ids
              AND link.deleted_at IS NULL
            ORDER BY link.workspace_id
            LIMIT 1
        ) AS filename,
        embedding.qdrant_point_id
    FROM document_chunks AS chunk
    JOIN chunk_embeddings AS embedding
      ON embedding.chunk_id = chunk.id
     AND embedding.origin_workspace_id = chunk.origin_workspace_id
     AND embedding.document_id = chunk.document_id
     AND embedding.processing_run_id = chunk.processing_run_id
    JOIN documents AS document
      ON document.id = chunk.document_id
    WHERE chunk.id IN :chunk_ids
      AND EXISTS (
          SELECT 1
          FROM document_workspaces AS link
          WHERE link.document_id = chunk.document_id
            AND link.workspace_id IN :workspace_ids
            AND link.deleted_at IS NULL
      )
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
        origin_workspace_id=Uuid(as_uuid=True),
        document_id=Uuid(as_uuid=True),
        processing_run_id=Uuid(as_uuid=True),
        section_path=JSON(),
        section_key=String(),
        is_heading_only=Boolean(),
        page_numbers=JSON(),
        filename=String(),
        qdrant_point_id=Uuid(as_uuid=True),
    )
)


def section_anchors(sources: Sequence[ContextSource]) -> list[str]:
    """Section keys worth completing, in the rank order of the chunks naming them.

    A heading is topically concentrated, so against a vague query it outscores the
    prose belonging to it: on a 57-page regulation document "Article 16: General
    framework" retrieved at 0.81 while Regulation 16.01-16.06 did not appear at all,
    and the agent reported the article's content as absent from a document that
    contained it.

    Reading order does not repair that, because the content is rarely adjacent. When
    Docling meets "Pasal 16" followed by numbered sub-provisions it files the prose
    under paths that *end* in "16.01", "16.02" and so on -- none of which equal the
    article's own key. Completing an anchor therefore means its own section plus
    every section beneath it, which is a hierarchy question rather than a positional
    one.

    Every retrieved chunk anchors, not only the bare headings. A parent heading whose
    intro was merged into one chunk with its lead paragraph is not marked as a
    heading, yet it is exactly the chunk whose sub-sections went missing.

    A chunk with no headings encodes to the empty array, which has no descendants, so
    it anchors only itself. In a document that is entirely headingless -- a badly
    scanned PDF, say -- that degrades to pulling the few chunks that share its key,
    which is the preamble the match came from and no worse than returning it alone.
    """
    anchors: list[str] = []
    seen: set[str] = set()
    for source in sources:
        key = source.section_key
        if not key or key in seen:
            continue
        seen.add(key)
        anchors.append(key)
    return anchors


def _section_clause(anchors: Sequence[str]) -> Any:
    """Build ``(key = a OR key LIKE a's descendants) OR ...`` for a bounded anchor set.

    Kept as an expression rather than interpolated SQL text so the values stay bound
    parameters and cannot carry identifiers out of the statement.
    """
    return or_(
        *(
            or_(
                DocumentChunk.section_key == key,
                DocumentChunk.section_key.like(subtree_pattern(key), escape=LIKE_ESCAPE),
            )
            for key in anchors
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
        self,
        query: str,
        workspace_ids: list[UUID],
        limit: int,
        *,
        budget: int | None = None,
    ) -> list[ContextSource]:
        """Return up to ``limit`` vector matches, plus the sections they belong to.

        ``budget`` is how many sources the caller can accept overall. It is
        distinct from ``limit`` because the two clamp different things: ``limit`` bounds
        how many similarity matches one search may contribute, while ``budget`` bounds
        the total. Without it, a full page of matches leaves no room to complete a
        section -- which is exactly the case that matters, since a section named
        without its content is only visible when the search returned its quota.
        """
        if not workspace_ids:
            raise RetrievalUnavailable(
                "A verified non-empty workspace scope is required"
            )
        if len(workspace_ids) != len(set(workspace_ids)):
            raise RetrievalUnavailable("The workspace scope contains duplicate IDs")

        width = self._settings.section_expand_chunks
        cap = limit if budget is None else min(limit + width, budget)

        try:
            vector = await self._embed_query(query)
            matches = await self._query_qdrant(vector, workspace_ids, limit)
            sources = await self._load_validated_chunks(matches, workspace_ids)
            completed = await self._complete_sections(sources, workspace_ids)
            room = max(0, cap - len(sources))
            return sources + completed[:room]
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
                            "key": "workspace_ids",
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
            point_workspaces = point_payload.get("workspace_ids")
            if not isinstance(point_workspaces, list) or not (
                allowed_workspace_ids & {str(value) for value in point_workspaces}
            ):
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
                "origin_workspace_id": str(UUID(str(row["origin_workspace_id"]))),
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
                    section_key=str(row["section_key"] or ""),
                    is_heading_only=bool(row["is_heading_only"]),
                    page_numbers=list(row["page_numbers"] or []),
                    text=str(row["text"]),
                    token_count=int(row["token_count"]),
                )
            )
        return sources

    async def _complete_sections(
        self,
        sources: Sequence[ContextSource],
        workspace_ids: list[UUID],
    ) -> list[ContextSource]:
        """Fill in the sections the matched chunks belong to, under the same validation.

        Every condition ``VALIDATE_CHUNKS`` enforces is repeated here -- workspace scope,
        non-deleted document, active processing run, indexed embedding, matching content
        hash -- because these rows are not backed by a Qdrant match in this search and
        would otherwise reach the model unvetted.

        Each anchor takes at most ``section_expand_chunks``. A large section must not
        starve the anchors ranked below it just because its own chunks come first in
        reading order.
        """
        anchors = section_anchors(sources)
        width = self._settings.section_expand_chunks
        if not anchors or width <= 0:
            return []

        existing = {source.chunk_id for source in sources}
        link_workspace = (
            select(DocumentWorkspace.workspace_id)
            .where(
                DocumentWorkspace.document_id == DocumentChunk.document_id,
                DocumentWorkspace.workspace_id.in_(workspace_ids),
                DocumentWorkspace.deleted_at.is_(None),
            )
            .order_by(DocumentWorkspace.workspace_id)
            .limit(1)
            .scalar_subquery()
        )
        link_filename = (
            select(DocumentWorkspace.filename)
            .where(
                DocumentWorkspace.document_id == DocumentChunk.document_id,
                DocumentWorkspace.workspace_id.in_(workspace_ids),
                DocumentWorkspace.deleted_at.is_(None),
            )
            .order_by(DocumentWorkspace.workspace_id)
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            select(
                DocumentChunk.id,
                link_workspace.label("workspace_id"),
                DocumentChunk.document_id,
                DocumentChunk.processing_run_id,
                DocumentChunk.ordinal,
                DocumentChunk.text,
                DocumentChunk.token_count,
                DocumentChunk.content_hash,
                DocumentChunk.section_path,
                DocumentChunk.section_key,
                DocumentChunk.page_numbers,
                link_filename.label("filename"),
            )
            .join(
                ChunkEmbedding,
                and_(
                    ChunkEmbedding.chunk_id == DocumentChunk.id,
                    ChunkEmbedding.origin_workspace_id == DocumentChunk.origin_workspace_id,
                    ChunkEmbedding.document_id == DocumentChunk.document_id,
                    ChunkEmbedding.processing_run_id
                    == DocumentChunk.processing_run_id,
                ),
            )
            .join(
                Document,
                Document.id == DocumentChunk.document_id,
            )
            .where(
                _section_clause(anchors),
                exists().where(
                    DocumentWorkspace.document_id == DocumentChunk.document_id,
                    DocumentWorkspace.workspace_id.in_(workspace_ids),
                    DocumentWorkspace.deleted_at.is_(None),
                ),
                Document.deleted_at.is_(None),
                Document.active_processing_run_id == DocumentChunk.processing_run_id,
                ChunkEmbedding.embedding_profile == self._settings.embedding_profile,
                ChunkEmbedding.model == self._settings.embedding_model,
                ChunkEmbedding.dimensions == self._settings.embedding_dimensions,
                ChunkEmbedding.status == EMBEDDING_INDEXED,
                ChunkEmbedding.content_hash == DocumentChunk.content_hash,
            )
            # A chunk's own text first, its sub-sections after. The parent's prose is
            # what answers "what does this article say"; the sub-sections only add the
            # detail, so they should not displace it when the budget runs short.
            .order_by(
                case((DocumentChunk.section_key.in_(anchors), 0), else_=1),
                DocumentChunk.ordinal,
            )
        )

        async with self._sessions() as session:
            result = await session.execute(statement)
            rows = result.all()

        added: list[ContextSource] = []
        taken_per_anchor: dict[str, int] = dict.fromkeys(anchors, 0)
        for row in rows:
            chunk_id = row[0]
            if chunk_id in existing:
                continue
            section_key = str(row[9])
            # Attribute the row to the first anchor that claims it. Anchors are
            # deduped but one section can nest inside another anchor, and the
            # innermost match is the one that should be charged for the row.
            owner = next(
                (
                    key
                    for key in anchors
                    if section_descends_from(section_key, key)
                ),
                None,
            )
            if owner is None:
                raise RetrievalUnavailable(
                    "A completed section row does not descend from any anchor"
                )
            if taken_per_anchor[owner] >= width:
                continue
            taken_per_anchor[owner] += 1
            text_value = str(row[5])
            if hashlib.sha256(text_value.encode()).hexdigest() != row[7]:
                raise RetrievalUnavailable(
                    "PostgreSQL section chunk text does not match its content hash"
                )
            added.append(
                ContextSource(
                    workspace_id=row[1],
                    document_id=row[2],
                    processing_run_id=row[3],
                    chunk_id=chunk_id,
                    ordinal=int(row[4]),
                    # Completed rows were not matched on similarity, so they inherit
                    # the relevance floor: attributed, not ranked. A low value keeps
                    # them from outranking genuine vector matches downstream.
                    score=self._settings.min_relevance_score,
                    content_hash=str(row[7]),
                    filename=row[11],
                    section_path=list(row[8] or []),
                    section_key=section_key,
                    page_numbers=list(row[10] or []),
                    text=text_value,
                    token_count=int(row[6]),
                )
            )
            existing.add(chunk_id)
        return added

    async def aclose(self) -> None:
        await self._embedding_client.aclose()
        await self._qdrant_client.aclose()
        await self._engine.dispose()
