import json
from html import escape
from uuid import UUID

from app.retrieval.schemas import ContextSource


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
        sections.append(
            f"[{index}] filename={filename} workspace_id={source.workspace_id} "
            f"document_id={source.document_id} chunk_id={source.chunk_id} pages={pages}\n"
            f"<untrusted_document_text>\n{escape(source.text)}\n</untrusted_document_text>"
        )
    return "\n\n".join(sections)


def source_indices(sources: dict[UUID, ContextSource]) -> dict[UUID, int]:
    return {chunk_id: index for index, chunk_id in enumerate(sources, start=1)}
