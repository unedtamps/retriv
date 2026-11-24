from app.db.models import VERSION_RELATION_PERIODIC_SERIES
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
            return (
                f"No document matching {pattern!r} (by filename or metadata) "
                "is in scope for this API key.\n\n"
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
        if summary.primary_entities:
            entities = ", ".join(summary.primary_entities[:2])
            details.append(f'entity="{entities}"')
        doc_kind = summary.doc_subtype or (
            summary.doc_category.replace("_", " ")
            if summary.doc_category and summary.doc_category != "other"
            else None
        )
        if doc_kind:
            details.append(f'type="{doc_kind}"')
        if summary.period_label:
            details.append(f'period="{summary.period_label}"')
        if summary.version_signal is not None:
            sig = summary.version_signal
            if sig.is_superseded:
                details.append(
                    f'version="superseded (latest: {sig.latest_filename} '
                    f'[{sig.latest_document_id}])"'
                )
            elif sig.relation_kind == VERSION_RELATION_PERIODIC_SERIES:
                suffix = ", latest" if sig.is_latest else ""
                details.append(
                    f'version="series ({sig.version_rank} of {sig.version_count}{suffix})"'
                )
            elif sig.is_latest:
                details.append(f'version="latest (v{sig.version_rank} of {sig.version_count})"')
        if summary.is_row_stored:
            details.append(f"{summary.row_count} rows")
            details.append(
                "queryable with query_rows" if queryable else "row queries are not enabled"
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
            "get_document_link, or to query_rows for a CSV, workbook or JSON data file.\n"
        )
    return body + "\n\n"
