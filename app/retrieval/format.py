import json
from html import escape
from uuid import UUID

from app.retrieval.navigation import DocumentOutline
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
        section = (
            f" section={json.dumps(source.section_path, ensure_ascii=True)}"
            if source.section_path
            else ""
        )
        kind = " kind=heading" if source.is_heading_only else ""
        sections.append(
            f"[{index}] filename={filename}{section}{kind} ordinal={source.ordinal} "
            f"workspace_id={source.workspace_id} "
            f"document_id={source.document_id} chunk_id={source.chunk_id} pages={pages}\n"
            f"<untrusted_document_text>\n{escape(source.text)}\n</untrusted_document_text>"
        )
    return "\n\n".join(sections)


def navigation_hint(sources: list[ContextSource], indices: dict[UUID, int]) -> str:
    """Tell the reader exactly what to call when a hit is a heading, or "".

    Only rendered when a heading-only chunk was actually returned. A hint shown
    on every search would be background noise within two queries, and a reader
    that learns to ignore it cannot be taught anything by the version that matters.

    The wording agrees with the count in all four combinations. The total is
    already stated, so the hint names only the first heading's position rather
    than repeating how many others there are: a reader that reads "3 of 3
    sources" and then "and 2 more" cannot tell which number to trust.
    """
    headings = [source for source in sources if source.is_heading_only]
    if not headings:
        return ""
    first = headings[0]
    label = f"source [{indices[first.chunk_id]}]"
    count = len(headings)
    total = len(sources)
    if count == 1:
        subject = f"1 of {total} sources is a heading with no text of its own"
        verb = "this source is"
    else:
        subject = f"{count} of {total} sources are headings with no text of their own"
        verb = "these sources are"
    return (
        f"{subject}. A heading is a signpost: the wording it points at is "
        f"elsewhere in the document, so {verb} not evidence that the document is "
        f"silent on the subject. Call "
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


def source_indices(
    sources: dict[UUID, ContextSource], *, start: int = 1
) -> dict[UUID, int]:
    return {chunk_id: index for index, chunk_id in enumerate(sources, start=start)}


def format_outline(outline: DocumentOutline) -> str:
    """Render a document's sections as the table of contents it never had.

    Each line carries the ordinal a read should start at, because the point of
    an outline is to enter the document somewhere rather than read it from the
    top. Nesting is shown by indentation, which is the only structure the
    section paths reliably share.
    """
    header = (
        f"filename={json.dumps(outline.filename, ensure_ascii=True)} "
        f"document_id={outline.document_id} chunks={outline.total_chunks} "
        f"pages={outline.page_count or 'unknown'} sections={outline.section_count}"
    )
    if not outline.has_headings:
        return (
            f"{header}\n"
            "No section headings were extracted from this document, so it has "
            "no outline. Read it in order with read_document instead."
        )

    lines = [header]
    for section in outline.sections:
        depth = max(0, len(section.section_path) - 1)
        label = section.section_path[-1] if section.section_path else section.section_key
        page = f"page {section.page}" if section.page is not None else "page unknown"
        lines.append(
            f"{'  ' * (depth + 1)}{label}  "
            f"[ordinal {section.ordinal}, {page}, {section.chunks} chunk(s)]"
        )
    if outline.truncated:
        lines.append(
            f"... showing {len(outline.sections)} of {outline.section_count} sections. "
            "Read from an ordinal above, or read_document from 0 to walk the document."
        )
    return "\n".join(lines)

