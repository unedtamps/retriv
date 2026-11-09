"""Sequential reading of a document, as opposed to searching it.

``ContextRetriever`` answers "which passage answers this question", which is a
semantic question and it answers it with a vector search. It cannot answer "what
comes after this chunk", because nothing in a vector search knows what comes after
anything. This module holds that other axis: ``ordinal`` is a chunk's position in
reading order within one processing run, so a window over it is a contiguous slice
of the document.

The two compose rather than compete. A search names the section that matters;
navigation reads it. A heading retrieved on its own -- ``kind=heading`` -- names a
section whose text is elsewhere in the document, and the ``ordinal`` in its label
is where reading from would start.

Every row here is loaded without a vector match behind it, exactly like the rows
``ContextRetriever`` completes from, so every condition ``VALIDATE_CHUNKS``
enforces is repeated: workspace scope, non-deleted document, active processing
run, indexed embedding of the configured profile, and a recomputed content hash.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, exists, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import (
    EMBEDDING_INDEXED,
    ChunkEmbedding,
    Document,
    DocumentChunk,
    DocumentWorkspace,
)
from app.retrieval.schemas import ContextSource, NavigationWindow
from app.retrieval.sql import ROW_STORED_MEDIA_TYPES


class DocumentNotFound(RuntimeError):
    """No readable document matches, or the caller may not see it.

    One message for both, deliberately: naming a document that exists elsewhere
    must be indistinguishable from naming one that does not exist.
    """


class DocumentNotNavigable(RuntimeError):
    """The document has no chunk stream to read.

    A CSV is stored as rows, not chunks, so reading it in reading order is not a
    thing this server can do. Saying so and naming the tool that can is more use
    to the caller than a generic failure.
    """


MAX_OUTLINE_CHUNKS = 20_000
"""How many chunk positions one outline may scan.

An outline reads positions, not text, so a row is a few dozen bytes; twenty
thousand of them is a document far longer than any the pipeline has seen and
still bounded. The cap is what keeps a pathological file from turning a
metadata call into a memory problem.
"""


@dataclass(frozen=True)
class OutlineSection:
    """One heading's place in a document, and how much text sits under it."""

    section_key: str
    section_path: list[str]
    ordinal: int
    page: int | None
    chunks: int


@dataclass(frozen=True)
class DocumentOutline:
    """A document's section tree, as positions rather than prose.

    ``sections`` is capped by the caller's limit; ``section_count`` is the
    number that exists, so a caller can say "showing 20 of 140" without
    counting the list it was handed.
    """

    filename: str
    document_id: UUID
    sections: list[OutlineSection]
    section_count: int
    total_chunks: int
    page_count: int
    truncated: bool

    @property
    def has_headings(self) -> bool:
        return self.section_count > 0


def _embedding_join() -> Any:
    """Pin a chunk to an embedding row belonging to the same run."""
    return and_(
        ChunkEmbedding.chunk_id == DocumentChunk.id,
        ChunkEmbedding.origin_workspace_id == DocumentChunk.origin_workspace_id,
        ChunkEmbedding.document_id == DocumentChunk.document_id,
        ChunkEmbedding.processing_run_id == DocumentChunk.processing_run_id,
    )


class DocumentNavigator:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._sessions = session_factory

    async def read_window(
        self,
        *,
        document_id: UUID,
        workspace_ids: list[UUID],
        start_ordinal: int = 0,
        limit: int = 20,
    ) -> NavigationWindow:
        """Return up to ``limit`` chunks from ``start_ordinal`` forward.

        Bounded twice: by ``limit`` from the caller and by
        ``navigation_token_budget`` from the server. The token bound is what
        keeps the response from becoming the problem ``get_document_link`` refuses
        to create, so it is applied while rows are accumulated rather than by
        trimming afterwards.
        """
        if not workspace_ids:
            raise DocumentNotFound("No readable document is in scope.")
        if len(workspace_ids) != len(set(workspace_ids)):
            raise DocumentNotFound("The workspace scope contains duplicate IDs.")
        if start_ordinal < 0 or limit < 1:
            raise ValueError("start_ordinal must be >= 0 and limit must be >= 1")

        try:
            async with self._sessions() as session:
                document = await session.scalar(
                    select(Document)
                    .join(
                        DocumentWorkspace,
                        DocumentWorkspace.document_id == Document.id,
                    )
                    .where(
                        Document.id == document_id,
                        DocumentWorkspace.workspace_id.in_(workspace_ids),
                        DocumentWorkspace.deleted_at.is_(None),
                    )
                )
                if document is None:
                    raise DocumentNotFound("No readable document matches that id in this scope.")
                if document.deleted_at is not None:
                    raise DocumentNotFound(
                        "That document is being deleted and can no longer be read."
                    )
                if document.detected_media_type in ROW_STORED_MEDIA_TYPES:
                    raise DocumentNotNavigable(
                        "That file is a CSV or workbook, which is stored as rows "
                        "rather than text. Use list_queryable_files to see its "
                        "columns, then query_rows to ask questions of the data."
                    )
                run_id = document.active_processing_run_id
                if run_id is None:
                    raise DocumentNotFound(
                        "That document is not indexed yet, so it has no text to read."
                    )

                total = await session.scalar(
                    select(func.count())
                    .select_from(DocumentChunk)
                    .where(DocumentChunk.processing_run_id == run_id)
                )
                rows = (
                    await session.execute(
                        self._window_query(run_id, start_ordinal, limit, workspace_ids)
                    )
                ).all()
        except (DocumentNotFound, DocumentNotNavigable):
            raise
        except (SQLAlchemyError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise DocumentNotFound("That document could not be read.") from exc

        return self._to_window(rows, total=int(total or 0))

    async def outline(
        self,
        *,
        document_id: UUID,
        workspace_ids: list[UUID],
        limit: int = 200,
    ) -> DocumentOutline:
        """The document's headings, each with the ordinal and page it starts at.

        Reading order is not a table of contents: it says where every chunk is,
        not where a section begins. A caller that wants "Bab 3" needs the
        latter, and the chunk rows already know it through ``section_key``.

        The embedding ledger is not joined. A section exists whether or not its
        chunks embedded, and an outline is metadata about the document rather
        than a passage from it -- hiding unembedded sections would describe a
        document that is not the one stored.
        """
        if not workspace_ids:
            raise DocumentNotFound("No readable document is in scope.")
        if len(workspace_ids) != len(set(workspace_ids)):
            raise DocumentNotFound("The workspace scope contains duplicate IDs.")
        if limit < 1:
            raise ValueError("limit must be >= 1")

        try:
            async with self._sessions() as session:
                row = (
                    await session.execute(
                        select(Document, DocumentWorkspace.filename)
                        .join(
                            DocumentWorkspace,
                            DocumentWorkspace.document_id == Document.id,
                        )
                        .where(
                            Document.id == document_id,
                            DocumentWorkspace.workspace_id.in_(workspace_ids),
                            DocumentWorkspace.deleted_at.is_(None),
                        )
                    )
                ).one_or_none()
                if row is None:
                    raise DocumentNotFound("No readable document matches that id in this scope.")
                document, filename = row
                if document.deleted_at is not None:
                    raise DocumentNotFound(
                        "That document is being deleted and can no longer be read."
                    )
                if document.detected_media_type in ROW_STORED_MEDIA_TYPES:
                    raise DocumentNotNavigable(
                        "That file is a CSV or workbook, which is stored as rows "
                        "rather than text. Use list_queryable_files to see its "
                        "columns, then query_rows to ask questions of the data."
                    )
                run_id = document.active_processing_run_id
                if run_id is None:
                    raise DocumentNotFound(
                        "That document is not indexed yet, so it has no sections to list."
                    )

                total = await session.scalar(
                    select(func.count())
                    .select_from(DocumentChunk)
                    .where(DocumentChunk.processing_run_id == run_id)
                )
                rows = (
                    await session.execute(
                        select(
                            DocumentChunk.section_key,
                            DocumentChunk.section_path,
                            DocumentChunk.ordinal,
                            DocumentChunk.page_numbers,
                        )
                        .where(DocumentChunk.processing_run_id == run_id)
                        .order_by(DocumentChunk.ordinal)
                        .limit(MAX_OUTLINE_CHUNKS)
                    )
                ).all()
        except (DocumentNotFound, DocumentNotNavigable):
            raise
        except (SQLAlchemyError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise DocumentNotFound("That document could not be outlined.") from exc

        return self._to_outline(
            rows,
            filename=filename,
            document_id=document_id,
            total=int(total or 0),
            limit=limit,
        )

    def _to_outline(
        self,
        rows: Sequence[Any],
        *,
        filename: str,
        document_id: UUID,
        total: int,
        limit: int,
    ) -> DocumentOutline:
        order: list[str] = []
        first: dict[str, tuple[list[str], int, int | None]] = {}
        counts: dict[str, int] = {}
        page_count = 0
        for section_key, section_path, ordinal, page_numbers in rows:
            pages = [int(page) for page in (page_numbers or [])]
            if pages:
                page_count = max(page_count, max(pages))
            path = list(section_path or [])
            if not path:
                # A chunk with no heading is preamble, not a section; it still
                # contributes to the page count but has no line in the outline.
                continue
            key = str(section_key or "")
            counts[key] = counts.get(key, 0) + 1
            if key not in first:
                first[key] = (
                    path,
                    int(ordinal),
                    pages[0] if pages else None,
                )
                order.append(key)

        keys = order
        sections = [
            OutlineSection(
                section_key=key,
                section_path=first[key][0],
                ordinal=first[key][1],
                page=first[key][2],
                chunks=counts[key],
            )
            for key in keys[:limit]
        ]
        return DocumentOutline(
            filename=filename,
            document_id=document_id,
            sections=sections,
            section_count=len(keys),
            total_chunks=total,
            page_count=page_count,
            truncated=len(keys) > limit or len(rows) < total,
        )

    def _window_query(
        self,
        run_id: UUID,
        start_ordinal: int,
        limit: int,
        workspace_ids: list[UUID],
    ) -> Select[tuple[Any, ...]]:
        embedding_join = _embedding_join()
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
        return (
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
            .join(ChunkEmbedding, embedding_join)
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(
                DocumentChunk.processing_run_id == run_id,
                DocumentChunk.ordinal >= start_ordinal,
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
            .order_by(DocumentChunk.ordinal)
            .limit(limit)
        )

    def _to_window(self, rows: Sequence[Any], *, total: int) -> NavigationWindow:
        budget = self._settings.navigation_token_budget
        sources: list[ContextSource] = []
        spent = 0
        for row in rows:
            token_count = int(row[6])
            chunk_text = str(row[5])
            if hashlib.sha256(chunk_text.encode()).hexdigest() != row[7]:
                raise DocumentNotFound("A stored chunk's text does not match its content hash.")
            if sources and spent + token_count > budget:
                break
            spent += token_count
            sources.append(
                ContextSource(
                    workspace_id=row[1],
                    document_id=row[2],
                    processing_run_id=row[3],
                    chunk_id=row[0],
                    ordinal=int(row[4]),
                    score=1.0,
                    content_hash=str(row[7]),
                    filename=row[12],
                    section_path=list(row[8] or []),
                    section_key=str(row[9] or ""),
                    is_heading_only=bool(row[10]),
                    page_numbers=list(row[11] or []),
                    text=chunk_text,
                    token_count=token_count,
                )
            )

        next_ordinal: int | None = None
        if sources:
            candidate = sources[-1].ordinal + 1
            if candidate < total:
                next_ordinal = candidate
        return NavigationWindow(sources=sources, total=total, next_ordinal=next_ordinal)

    async def aclose(self) -> None:
        """Nothing to release.

        The navigator borrows the session factory it was handed rather than
        owning an engine, so the application lifespan stays responsible for
        disposing the pool.
        """
