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
) -> str:
    """Render matches as text, one line per document.

    Follows the shape of the CSV catalogue rather than JSON: these are already
    small, a model reads a labelled line more reliably than it parses a blob,
    and it matches what the other tools return.

    Every line carries the workspace, size and date next to the name. Filenames
    are not unique, and a canonical document may appear once per linked
    workspace with that workspace's name. The document_id is what
    download_document and query_rows need anyway.
    """
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
        if summary.is_csv:
            details.append(f"{summary.row_count} rows")
            details.append("queryable with query_rows")
        lines.append("  " + "  ".join(details))

    if total > len(summaries):
        lines.append(f"  ... and {total - len(summaries)} more. Raise limit to see them.")
    elif total == 0:
        lines.append("  none yet")

    body = "\n".join(lines)
    if pattern is None:
        body += (
            "\nUse this when you need a document_id: pass it to "
            "download_document, or to query_rows for a CSV.\n"
        )
    return body + "\n\n"
