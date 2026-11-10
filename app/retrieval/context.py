import hashlib
import logging
import math
from collections.abc import Sequence
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    RowMapping,
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
    RetrievalSetting,
)
from app.embeddings.base import EmbeddingProviderError
from app.embeddings.openai_compatible import truncate_normalize
from app.embeddings.pgvector import vector_literal
from app.retrieval.rerank import LayaReranker, RerankConfig
from app.retrieval.schemas import ContextSource
from app.retrieval.sql import escape_like
from app.sections import LIKE_ESCAPE, section_descends_from, subtree_pattern

logger = logging.getLogger(__name__)

SEARCH_VECTORS = (
    text("""
    WITH nearest AS MATERIALIZED (
        SELECT
            embedding.chunk_id,
            embedding.content_hash AS embedding_content_hash,
            1 - (embedding.embedding <=> CAST(:query_vector AS vector)) AS score
        FROM chunk_embeddings AS embedding
        WHERE embedding.embedding_profile = :embedding_profile
          AND embedding.model = :embedding_model
          AND embedding.dimensions = :embedding_dimensions
          AND embedding.status = 'indexed'
          AND embedding.embedding IS NOT NULL
          AND (:document_id IS NULL OR embedding.document_id = :document_id)
          AND EXISTS (
              SELECT 1
              FROM document_workspaces AS link
              WHERE link.document_id = embedding.document_id
                AND link.workspace_id IN :workspace_ids
                AND link.deleted_at IS NULL
          )
        ORDER BY embedding.embedding <=> CAST(:query_vector AS vector)
        LIMIT :candidate_limit
    )
    SELECT
        nearest.score,
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
        ) AS filename
    FROM nearest
    JOIN document_chunks AS chunk
      ON chunk.id = nearest.chunk_id
    JOIN documents AS document
      ON document.id = chunk.document_id
    WHERE chunk.content_hash = nearest.embedding_content_hash
      AND document.deleted_at IS NULL
      AND document.active_processing_run_id = chunk.processing_run_id
      AND (:document_id IS NULL OR chunk.document_id = :document_id)
      AND EXISTS (
          SELECT 1
          FROM document_workspaces AS link
          WHERE link.document_id = chunk.document_id
            AND link.workspace_id IN :workspace_ids
            AND link.deleted_at IS NULL
      )
    ORDER BY nearest.score DESC
    LIMIT :limit
    """)
    .bindparams(
        bindparam("workspace_ids", type_=Uuid(as_uuid=True), expanding=True),
        bindparam("document_id", type_=Uuid(as_uuid=True)),
    )
    .columns(
        score=Float(),
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
    the extractor meets "Pasal 16" followed by numbered sub-provisions it files the
    prose under paths that *end* in "16.01", "16.02" and so on -- none of which
    equal the article's own key. Completing an anchor therefore means its own section plus
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


class ContextRetriever:
    def __init__(
        self,
        settings: Settings,
        engine: AsyncEngine,
        *,
        embedding_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._is_postgres = engine.dialect.name == "postgresql"
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        self._embedding_client = embedding_client or httpx.AsyncClient(
            base_url=f"{settings.embedding_base_url.rstrip('/')}/",
            headers={"Authorization": f"Bearer {settings.embedding_api_key.get_secret_value()}"},
            timeout=settings.embedding_timeout_seconds,
        )

    async def _rerank_config(self) -> RerankConfig | None:
        """The admin's optional rerank setting, or None when it is off.

        The row is read per search so an admin's change applies to the next
        tool call. A missing row is the normal default; a database error is
        treated as "off" rather than turning a retrieval into a failure.
        """
        try:
            async with self._sessions() as session:
                row = await session.get(RetrievalSetting, 1)
        except SQLAlchemyError:
            logger.warning("retrieval settings could not be read; reranking stays off")
            return None
        if row is None or not row.rerank_enabled:
            return None
        return RerankConfig(
            model=row.rerank_model,
            candidates=max(1, min(row.rerank_candidates, 100)),
            concurrency=max(1, min(row.rerank_concurrency, 16)),
            min_score=row.rerank_min_score,
            timeout_ms=max(100, min(row.rerank_timeout_ms, 60_000)),
        )

    async def _rerank(
        self,
        query: str,
        sources: list[ContextSource],
        *,
        limit: int,
        config: RerankConfig,
    ) -> list[ContextSource]:
        result = await LayaReranker(self._embedding_client, config).rerank(
            query, sources, limit=limit
        )
        return result.sources

    async def retrieve(
        self,
        query: str,
        workspace_ids: list[UUID],
        limit: int,
        *,
        budget: int | None = None,
        min_score: float | None = None,
        document_id: UUID | None = None,
    ) -> list[ContextSource]:
        """Return up to ``limit`` vector matches, plus the sections they belong to.

        ``budget`` is how many sources the caller can accept overall. It is
        distinct from ``limit`` because the two clamp different things: ``limit`` bounds
        how many similarity matches one search may contribute, while ``budget`` bounds
        the total. Without it, a full page of matches leaves no room to complete a
        section -- which is exactly the case that matters, since a section named
        without its content is only visible when the search returned its quota.

        ``min_score`` overrides the deployment's relevance floor for this call
        only; ``None`` keeps the configured ``min_relevance_score``.

        ``document_id`` narrows the search to one document; it must still be
        inside ``workspace_ids``. Section completion is narrowed with it, or a
        same-named section in another document could leak into the answer.
        """
        if not workspace_ids:
            raise RetrievalUnavailable("A verified non-empty workspace scope is required")
        if len(workspace_ids) != len(set(workspace_ids)):
            raise RetrievalUnavailable("The workspace scope contains duplicate IDs")

        width = self._settings.section_expand_chunks
        cap = limit if budget is None else min(limit + width, budget)
        floor = self._settings.min_relevance_score if min_score is None else min_score
        rerank = await self._rerank_config()
        pool = max(limit, rerank.candidates) if rerank is not None else limit

        try:
            vector = await self._embed_query(query)
            sources = await self._search_vectors(
                vector,
                workspace_ids,
                pool,
                min_score=floor,
                document_id=document_id,
            )
            completed = await self._complete_sections(
                sources,
                workspace_ids,
                min_score=floor,
                document_id=document_id,
            )
            if rerank is None:
                room = max(0, cap - len(sources))
                return sources + completed[:room]
            candidates = sources + completed[: max(0, pool - len(sources))]
            return await self._rerank(query, candidates, limit=cap, config=rerank)
        except (
            EmbeddingProviderError,
            httpx.HTTPError,
            SQLAlchemyError,
            IndexError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise RetrievalUnavailable("Scoped context retrieval failed validation") from exc

    async def search_text(
        self,
        query: str,
        workspace_ids: list[UUID],
        limit: int,
        *,
        document_id: UUID | None = None,
    ) -> list[ContextSource]:
        """Chunks containing ``query`` as literal text, not by meaning.

        An embedding answers "what is this about" and does that well, but it is
        the wrong instrument for an identifier: an invoice number, an article
        reference, a name. Those are strings, and a string search is exact
        where a vector is approximate.

        The rows are validated exactly as a vector match is -- live document,
        active run, workspace link, matching content hash -- but the embedding
        ledger is deliberately not joined. Text that failed to embed is still
        text a reader can use, and a lexical tool that refuses it would be
        hiding the one thing it exists to find.
        """
        if not workspace_ids:
            raise RetrievalUnavailable("A verified non-empty workspace scope is required")
        if len(workspace_ids) != len(set(workspace_ids)):
            raise RetrievalUnavailable("The workspace scope contains duplicate IDs")
        if not query.strip():
            raise RetrievalUnavailable("A non-empty text query is required")

        rerank = await self._rerank_config()
        pool = max(limit, min(rerank.candidates, 20)) if rerank is not None else limit
        pattern = f"%{escape_like(query)}%"
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
                DocumentChunk.is_heading_only,
                DocumentChunk.page_numbers,
                link_filename.label("filename"),
            )
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                DocumentChunk.text.ilike(pattern, escape="\\"),
                exists().where(
                    DocumentWorkspace.document_id == DocumentChunk.document_id,
                    DocumentWorkspace.workspace_id.in_(workspace_ids),
                    DocumentWorkspace.deleted_at.is_(None),
                ),
                Document.deleted_at.is_(None),
                Document.active_processing_run_id == DocumentChunk.processing_run_id,
            )
            .order_by(
                case((DocumentChunk.text.like(pattern, escape="\\"), 0), else_=1),
                DocumentChunk.document_id,
                DocumentChunk.ordinal,
            )
            .limit(pool)
        )
        if document_id is not None:
            statement = statement.where(DocumentChunk.document_id == document_id)

        try:
            async with self._sessions() as session:
                rows = (await session.execute(statement)).all()
        except (SQLAlchemyError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise RetrievalUnavailable("Scoped text search failed validation") from exc
        sources = self._text_sources(rows)
        if rerank is None:
            return sources
        return await self._rerank(query, sources, limit=limit, config=rerank)

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
            or len(raw_vector) < self._settings.embedding_dimensions
        ):
            raise ValueError("Embedding response dimensions do not match the configured profile")
        vector: list[float] = []
        for value in raw_vector:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError("Embedding response contains a non-numeric vector value")
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("Embedding response contains a non-finite vector value")
            vector.append(number)
        return truncate_normalize(vector, self._settings.embedding_dimensions)

    async def _search_vectors(
        self,
        vector: list[float],
        workspace_ids: list[UUID],
        limit: int,
        *,
        min_score: float,
        document_id: UUID | None,
    ) -> list[ContextSource]:
        """The nearest ``limit`` indexed chunks, servable by construction.

        One statement does the work the two-store design split in two. The
        join is the validation: a row can only come back if its document is
        live, its run is the document's active one, its embedding is indexed
        under the current profile with the configured dimensions, its hash
        matches the chunk's, and a workspace link puts it inside the signed
        scope. The payload metadata a Qdrant match used to carry and be
        checked against is simply not needed -- the row is its own payload.

        The ordering is on raw cosine distance because that is what lets the
        planner use the HNSW index. The relevance floor is applied after the
        fetch, so the semantics match what Qdrant's were: the index returns
        the nearest ``limit`` rows and anything under the floor is dropped
        here. A match that falls below it costs a slot, exactly as before.
        """
        rows = await self._fetch_vector_rows(
            vector, workspace_ids, limit, document_id=document_id
        )
        return self._validated_sources(rows, min_score=min_score)

    async def _fetch_vector_rows(
        self,
        vector: list[float],
        workspace_ids: list[UUID],
        limit: int,
        *,
        document_id: UUID | None,
    ) -> list[RowMapping]:
        candidate_limit = min(
            max(limit * self._settings.retrieval_candidate_multiplier, 32),
            1_000,
        )
        async with self._sessions() as session:
            if self._is_postgres:
                # The nearest-first CTE only helps if the planner takes the
                # HNSW path. On a small corpus it otherwise estimates a
                # sequential scan plus sort as cheaper; forcing the ordered
                # index path is what makes the candidate list the driving
                # relation. These are transaction-local and this session runs
                # one retrieval query.
                await session.execute(
                    text(
                        "SELECT "
                        "set_config('jit', 'off', true), "
                        "set_config('enable_sort', 'off', true), "
                        "set_config('enable_seqscan', 'off', true), "
                        "set_config('enable_bitmapscan', 'off', true), "
                        "set_config('hnsw.ef_search', :ef_search, true)"
                    ),
                    {"ef_search": str(candidate_limit)},
                )
            result = await session.execute(
                SEARCH_VECTORS,
                {
                    "query_vector": vector_literal(vector),
                    "workspace_ids": workspace_ids,
                    "embedding_profile": self._settings.embedding_profile,
                    "embedding_model": self._settings.embedding_model,
                    "embedding_dimensions": self._settings.embedding_dimensions,
                    "document_id": document_id,
                    "candidate_limit": candidate_limit,
                    "limit": limit,
                },
            )
            return list(result.mappings())

    def _validated_sources(
        self, rows: Sequence[RowMapping], *, min_score: float
    ) -> list[ContextSource]:
        sources: list[ContextSource] = []
        for row in rows:
            score = float(row["score"])
            if not math.isfinite(score) or score < min_score:
                continue
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
                    score=score,
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

    def _text_sources(self, rows: Sequence[Any]) -> list[ContextSource]:
        """Text-search rows in the same shape a vector match produces.

        The score is 1.0 rather than a similarity: a substring either is
        present or is not, and inventing a number between those two would
        invite a caller to compare it against the relevance floor, which does
        not apply here.
        """
        sources: list[ContextSource] = []
        for row in rows:
            text_value = str(row[5])
            if hashlib.sha256(text_value.encode()).hexdigest() != row[7]:
                raise RetrievalUnavailable(
                    "PostgreSQL chunk text does not match its content hash"
                )
            sources.append(
                ContextSource(
                    workspace_id=UUID(str(row[1])),
                    document_id=UUID(str(row[2])),
                    processing_run_id=UUID(str(row[3])),
                    chunk_id=UUID(str(row[0])),
                    ordinal=int(row[4]),
                    score=1.0,
                    content_hash=str(row[7]),
                    filename=row[12],
                    section_path=list(row[8] or []),
                    section_key=str(row[9] or ""),
                    is_heading_only=bool(row[10]),
                    page_numbers=list(row[11] or []),
                    text=text_value,
                    token_count=int(row[6]),
                )
            )
        return sources

    async def _complete_sections(
        self,
        sources: Sequence[ContextSource],
        workspace_ids: list[UUID],
        *,
        min_score: float,
        document_id: UUID | None,
    ) -> list[ContextSource]:
        """Fill in the sections the matched chunks belong to, under the same validation.

        Every condition the vector search's join enforces is repeated here -- workspace
        scope, non-deleted document, active processing run, indexed embedding, matching
        content hash -- because these rows are not backed by a match in this search and
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
                    ChunkEmbedding.processing_run_id == DocumentChunk.processing_run_id,
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
            .order_by(
                case((DocumentChunk.section_key.in_(anchors), 0), else_=1),
                DocumentChunk.ordinal,
            )
        )
        if document_id is not None:
            statement = statement.where(DocumentChunk.document_id == document_id)

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
            owner = next(
                (key for key in anchors if section_descends_from(section_key, key)),
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
                    score=min_score,
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
        await self._engine.dispose()
