import json
from html import escape
from uuid import UUID

from app.retrieval.schemas import ContextSource, NavigationWindow


def format_context(sources: list[ContextSource], indices: dict[UUID, int]) -> str:
    """Render retrieved chunks as labelled, explicitly-untrusted blocks.

    The label prefix ``[n]`` is what a citation refers to; the surrounding
    ``<untrusted_document_text>`` tags tell the reader that the body is
    source data, not instructions.
    """
    sections: list[str] = []
    for source in sources:
        index = indices[source.chunk_id]
        pages = ", ".join(str(page) for page in source.page_numbers) or "unknown"
        filename = json.dumps(source.filename or "unknown", ensure_ascii=True)
        # The heading path is what lets the reader tell "Pasal 16" from
        # "Pasal 16 of a different regulation". Omitted when the source has no
        # section, which is the case for CSV rows.
        section = (
            f" section={json.dumps(source.section_path, ensure_ascii=True)}"
            if source.section_path
            else ""
        )
        # A bare heading is a signpost, not an answer. Saying so stops the reader
        # treating a source that reads like a title as the document having no
        # content, which is the misreading section completion exists to prevent.
        kind = " kind=heading" if source.is_heading_only else ""
        # ordinal is what read_document takes as start_ordinal. Without it a
        # reader that wants to continue from a hit has to guess where to start,
        # which is the guessing this label exists to end.
        sections.append(
            f"[{index}] filename={filename}{section}{kind} ordinal={source.ordinal} "
            f"workspace_id={source.workspace_id} "
            f"document_id={source.document_id} chunk_id={source.chunk_id} pages={pages}\n"
            f"<untrusted_document_text>\n{escape(source.text)}\n</untrusted_document_text>"
        )
    return "\n\n".join(sections)


def navigation_hint(
    sources: list[ContextSource], indices: dict[UUID, int]
) -> str:
    """Tell the reader exactly what to call when a hit is a heading, or "".

    Only rendered when a heading-only chunk was actually returned. A hint shown
    on every search would be background noise within two queries, and a reader
    that learns to ignore it cannot be taught anything by the version that matters.
    """
    headings = [source for source in sources if source.is_heading_only]
    if not headings:
        return ""
    first = headings[0]
    label = f"source [{indices[first.chunk_id]}]"
    plural = "s are" if len(headings) > 1 else " is"
    others = f" and {len(headings) - 1} more" if len(headings) > 1 else ""
    return (
        f"{len(headings)} of {len(sources)} sources{plural} a heading with no text "
        f"of its own{others}. A heading is a signpost: the wording it points at is "
        f"elsewhere in the document, so this source is not evidence that the "
        f"document is silent on the subject. Call "
        f"read_document(document_id={first.document_id}, "
        f"start_ordinal={first.ordinal}) to read forward from {label}."
    )


def navigation_footer(window: NavigationWindow) -> str:
    """State what was left out and where to resume.

    Without this a truncated window reads as the whole document, and a reader
    that does not know more exists will answer from the first page of a long one.
    """
    if not window.sources:
        return f"No chunks at or after ordinal {window.total}."
    shown = len(window.sources)
    if window.next_ordinal is None:
        return f"— {shown} of {window.total} chunks, end of document. —"
    return (
        f"— {shown} of {window.total} chunks. "
        f"More follow: call read_document again with start_ordinal="
        f"{window.next_ordinal}. —"
    )


def source_indices(sources: dict[UUID, ContextSource]) -> dict[UUID, int]:
    return {chunk_id: index for index, chunk_id in enumerate(sources, start=1)}
