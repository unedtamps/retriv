"""Finding a document by name, for a caller that only has the name.

Retrieval answers "what does this workspace say about X". This answers the
question before it: "which file is that in". Without it a model can only
learn a document_id from a search result or the CSV catalogue, so naming a
file in a question is not something it can act on.

The search is deliberately plain -- a case-insensitive substring of the stored
filename -- because a model guesses at names rather than recalling them, and a
fuzzy or semantic match here would return documents the caller never asked
about. Refusing to guess is the point.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Document, ProcessingRun
from app.processing.service import derived_document_status


def _escape_like(value: str) -> str:
    """Stop a search term acting as a wildcard.

    The term is a bind parameter, so it cannot inject SQL; without this it
    could still carry ``%`` and turn a lookup for one name into every document
    in the workspace, which reads as a correct answer to a question nobody
    asked.
    """
    for character in ("\\", "%", "_"):
        value = value.replace(character, "\\" + character)
    return value


@dataclass(frozen=True)
class DocumentSummary:
    """One document, with enough beside its name to tell two apart."""

    document_id: UUID
    workspace_id: UUID
    filename: str
    media_type: str
    size_bytes: int
    status: str
    row_count: int
    created_at: datetime

    @property
    def is_csv(self) -> bool:
        return self.filename.lower().endswith(".csv")


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

    Ordering puts an exact filename first, then the shortest names, because a
    revised document usually keeps its name and grows a suffix -- and the
    caller asking for "Peraturan.pdf" wants that file, not the 63 others whose
    names happen to contain the words. The active run is outer-joined rather
    than fetched per row for the same reason the list route does it: a status
    per document, and one round trip.
    """
    if not workspace_ids:
        return [], 0

    condition = Document.workspace_id.in_(workspace_ids) & Document.deleted_at.is_(None)
    if pattern:
        condition = condition & Document.original_filename.ilike(
            f"%{_escape_like(pattern)}%", escape="\\"
        )

    total = await session.scalar(
        select(func.count()).select_from(Document).where(condition)
    )

    # Ranked in Python rather than SQL: the ranking is "how close is this
    # name", which is not expressible as a portable ORDER BY, and the row count
    # is already bounded by the workspace's document count.
    result = await session.execute(
        select(Document, ProcessingRun)
        .outerjoin(ProcessingRun, ProcessingRun.id == Document.active_processing_run_id)
        .where(condition)
        .order_by(Document.created_at.desc(), Document.id)
    )

    rows = result.all()
    summaries = [
        DocumentSummary(
            document_id=document.id,
            workspace_id=document.workspace_id,
            filename=document.original_filename,
            media_type=document.detected_media_type,
            size_bytes=document.size_bytes,
            status=derived_document_status(document, run),
            row_count=int(run.row_count or 0) if run is not None else 0,
            created_at=document.created_at,
        )
        for document, run in rows
    ]

    if pattern:
        needle = pattern.casefold()
        summaries.sort(key=lambda item: (_rank(item.filename, needle), item.filename))

    return summaries[:limit], total or 0


def _rank(filename: str, needle: str) -> int:
    """0 for the exact name, 1 for a name that starts with it, else 2."""
    folded = filename.casefold()
    if folded == needle:
        return 0
    if folded.startswith(needle):
        return 1
    return 2


def format_documents(
    summaries: list[DocumentSummary],
    *,
    total: int,
    pattern: str | None,
) -> str:
    """Render matches as text, one line per document.

    Follows the shape of the CSV catalogue rather than JSON: these are already
    small, a model reads a labelled line more reliably than it parses a blob,
    and it matches what the other tools return.

    Every line carries the workspace, size and date next to the name. Filenames
    are not unique -- only a document's content is, per workspace -- so two
    revisions of one file would otherwise be two indistinguishable lines, and
    the document_id is what download_document and query_rows need anyway.
    """
    if not summaries:
        if pattern:
            return (
                f"No document whose name contains {pattern!r} is in scope for "
                "this API key.\n\n"
            )
        return "No documents are in scope for this API key.\n\n"

    header = f"Documents matching {pattern!r}:" if pattern else "Documents:"
    lines = [header]
    for summary in summaries:
        details = [
            f"[{summary.document_id}]",
            summary.filename,
            f"workspace_id={summary.workspace_id}",
            f"status={summary.status}",
            f"{summary.size_bytes} bytes",
        ]
        if summary.is_csv:
            details.append(f"{summary.row_count} rows")
            details.append("queryable with query_rows")
        lines.append("  " + "  ".join(details))

    if total > len(summaries):
        lines.append(
            f"  ... and {total - len(summaries)} more. Raise limit to see them."
        )
    elif total == 0:
        lines.append("  none yet")

    body = "\n".join(lines)
    if pattern is None:
        body += (
            "\nUse this when you need a document_id: pass it to "
            "download_document, or to query_rows for a CSV.\n"
        )
    return body + "\n\n"