"""Finding a document by name, for a caller that only has the name.

Retrieval answers "what does this workspace say about X". This answers the
question before it: "which file is that in". Without it a model can only
learn a document_id from a search result or the CSV catalogue, so naming a
file in a question is not something it can act on.

The query itself lives in app/documents/search.py, shared with the dashboard's
cross-workspace search route, so the two can never disagree about scope,
ranking or what counts as a match.
"""

from app.documents.search import DocumentSummary, find_documents

__all__ = ["DocumentSummary", "find_documents", "format_documents"]


def format_documents(
    summaries: list[DocumentSummary],
    *,
    total: int,
    pattern: str | None,
    queryable: bool = True,
) -> str:
    """Render document matches as one labelled line per document."""
    if not summaries:
        if pattern:
            return f"No document whose name contains {pattern!r} is in scope for this API key.\n\n"
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
        if summary.is_row_stored:
            details.append(f"{summary.row_count} rows")
            details.append(
                "queryable with query_rows"
                if queryable
                else "row queries are not enabled"
            )
        lines.append("  " + "  ".join(details))

    if total > len(summaries):
        lines.append(f"  ... and {total - len(summaries)} more. Raise limit to see them.")
    elif total == 0:
        lines.append("  none yet")

    body = "\n".join(lines)
    if pattern is None:
        body += (
            "\nUse this when you need a document_id: pass it to "
            "download_document, or to query_rows for a CSV or workbook.\n"
        )
    return body + "\n\n"
