"""Workspace and document overviews for the read tools.

``find_documents`` answers "which file is that". These two answer the questions
before and after it: which workspaces exist at all, and what a document
actually turned into once the pipeline had it. Both are metadata, not content,
so they are rendered as labelled lines rather than JSON -- the same shape the
catalogue and the document list use, and the one a model reads most reliably.
"""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class WorkspaceSummary:
    """One workspace the caller may read, and how much is in it."""

    workspace_id: UUID
    slug: str
    name: str
    role: str
    description: str | None
    documents: int


def format_workspaces(entries: list[WorkspaceSummary]) -> str:
    """Render the workspaces a credential can search.

    Without this the model only ever sees a workspace_id on a document line, so
    "which workspace has the procurement files" is a question it can describe
    but not answer. The slug is shown next to the name because users tend to
    say the short one.
    """
    if not entries:
        return "No workspaces are in scope for this API key."
    lines = [f"Workspaces ({len(entries)}):"]
    for entry in entries:
        line = (
            f"  [{entry.workspace_id}] {entry.slug} — {entry.name} "
            f"({entry.role})  {entry.documents} document(s)"
        )
        if entry.description:
            line += f"  {entry.description}"
        lines.append(line)
    return "\n".join(lines)


@dataclass(frozen=True)
class DocumentReport:
    """What the pipeline made of one document, for the model to reason about."""

    filename: str
    document_id: UUID
    workspace_name: str
    workspace_id: UUID
    media_type: str
    size_bytes: int
    uploaded_at: datetime | None
    status: str
    pipeline: str | None
    chunks: int
    embedded_chunks: int
    pages: int
    sections: int
    images: int
    described_images: int
    rows: int
    columns: list[str]
    last_error: str | None
    queryable: bool
    row_stored: bool
    tables: list[tuple[str, list[str], int]] = field(default_factory=list)


def format_document_report(report: DocumentReport) -> str:
    """Render extraction health so "not found" cannot mean "not readable".

    ``find_documents`` reports a status, which is enough to know whether to
    search and not enough to know what happened. A scan without OCR is
    ``ready`` and returns nothing, and that is the failure this report exists
    to make visible: pages and chunk counts are facts about the extraction,
    not about the subject.
    """
    lines = [
        f"filename: {report.filename}",
        f"document_id: {report.document_id}",
        f"workspace: {report.workspace_name} [{report.workspace_id}]",
        f"media_type: {report.media_type}",
        f"size_bytes: {report.size_bytes}",
    ]
    if report.uploaded_at is not None:
        lines.append(f"uploaded: {report.uploaded_at.date().isoformat()}")
    lines.append(f"status: {report.status}")
    if report.pipeline:
        lines.append(f"pipeline: {report.pipeline}")

    if report.row_stored:
        lines.append(f"rows: {report.rows}")
        if report.tables:
            for name, table_columns, table_rows in report.tables:
                label = name or "(root)"
                columns = ", ".join(table_columns) if table_columns else "unknown"
                lines.append(f"table {label}: {table_rows} rows; columns: {columns}")
        else:
            columns = ", ".join(report.columns) if report.columns else "unknown"
            lines.append(f"columns: {columns}")
        lines.append(
            "queryable with query_rows"
            if report.queryable
            else "row queries are not enabled on this deployment"
        )
    else:
        lines.append(f"chunks: {report.chunks} ({report.embedded_chunks} embedded)")
        lines.append(f"pages: {report.pages or 'unknown'}")
        lines.append(f"sections: {report.sections}")
        lines.append(f"images: {report.images} ({report.described_images} described)")

    if report.last_error:
        lines.append(f"last_error: {report.last_error}")
    if report.chunks == 0 and not report.row_stored and report.status == "ready":
        lines.append(
            "warning: no text chunks were extracted, so searching this document "
            "cannot find anything. A scanned PDF without a text layer produces "
            "this; re-ingesting it with OCR is the fix."
        )
    return "\n".join(lines)
