"""Finding a document by name, across the workspaces a caller may read.

Retrieval answers "what does this workspace say about X". This answers the
question before it: "which file is that in". Without it a model can only learn
a document_id from a search result or the CSV catalogue, so naming a file in a
question is not something it can act on.

The search is deliberately plain -- a case-insensitive substring of the stored
filename -- because a model guesses at names rather than recalling them, and a
fuzzy or semantic match here would return documents the caller never asked
about. Refusing to guess is the point.

Both callers share this query: the MCP ``find_documents`` tool and the
dashboard's cross-workspace search route. The scope is always a list of
workspace ids the caller may read, resolved by the caller -- never a filter
applied to the answer afterwards.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, DocumentWorkspace, ProcessingRun, Workspace
from app.processing.service import derived_document_status
from app.retrieval.sql import ROW_STORED_MEDIA_TYPES, escape_like


@dataclass(frozen=True)
class DocumentSummary:
    """One document, with enough beside its name to tell two apart."""

    document_id: UUID
    workspace_id: UUID
    workspace_name: str
    filename: str
    media_type: str
    size_bytes: int
    status: str
    row_count: int
    created_at: datetime

    @property
    def is_row_stored(self) -> bool:
        """Whether this file's content is rows rather than text.

        A workbook is stored exactly like a CSV -- one row per record, one
        column per header -- so a listing that offers one to ``query_rows``
        has to offer the other. Keyed on the media type the pipeline detected,
        not on the filename: a workbook is queryable whatever it was called.
        """
        return self.media_type in ROW_STORED_MEDIA_TYPES


async def find_documents(
    session: AsyncSession,
    workspace_ids: list[UUID],
    *,
    pattern: str | None,
    limit: int,
) -> tuple[list[DocumentSummary], int]:
    """Documents in ``workspace_ids`` whose filename contains ``pattern``.

    Returns the page and the total that matched before the page, so the caller
    can say "showing 20 of 63" instead of implying the list is complete.

    Ordering puts an exact filename first, then names that start with the
    pattern, because a revised document usually keeps its name and grows a
    suffix -- and the caller asking for "Peraturan.pdf" wants that file, not
    the 63 others whose names happen to contain the words. Ranking, the page
    slice and the total (a window count riding along with the page) all happen
    in one query, so a workspace with many matching documents loads only the
    page it asked for. The workspace name and the active run are joined rather
    than fetched per row for the same reason the list route does it: a label
    and a status per document, in one round trip.
    """
    if not workspace_ids:
        return [], 0

    condition = (
        DocumentWorkspace.workspace_id.in_(workspace_ids)
        & DocumentWorkspace.deleted_at.is_(None)
        & Document.deleted_at.is_(None)
    )
    statement = (
        select(
            Document,
            DocumentWorkspace,
            ProcessingRun,
            Workspace.name,
            func.count().over().label("total"),
        )
        .join(DocumentWorkspace, DocumentWorkspace.document_id == Document.id)
        .join(Workspace, Workspace.id == DocumentWorkspace.workspace_id)
        .outerjoin(ProcessingRun, ProcessingRun.id == Document.active_processing_run_id)
        .where(condition)
    )

    if pattern:
        needle = pattern.casefold()
        statement = statement.where(
            DocumentWorkspace.filename.ilike(f"%{escape_like(pattern)}%", escape="\\")
        )
        rank = case(
            (func.lower(DocumentWorkspace.filename) == needle, 0),
            (
                DocumentWorkspace.filename.ilike(f"{escape_like(needle)}%", escape="\\"),
                1,
            ),
            else_=2,
        )
        statement = statement.order_by(rank, DocumentWorkspace.filename, Document.id)
    else:
        statement = statement.order_by(DocumentWorkspace.created_at.desc(), Document.id)

    result = await session.execute(statement.limit(limit))

    rows = result.all()
    summaries = [
        DocumentSummary(
            document_id=document.id,
            workspace_id=link.workspace_id,
            workspace_name=workspace_name,
            filename=link.filename,
            media_type=document.detected_media_type,
            size_bytes=document.size_bytes,
            status=derived_document_status(document, run),
            row_count=int(run.row_count or 0) if run is not None else 0,
            created_at=link.created_at,
        )
        for document, link, run, workspace_name, _ in rows
    ]

    return summaries, int(rows[0].total) if rows else 0
