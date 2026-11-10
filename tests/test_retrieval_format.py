"""The label and hint text around retrieved sources.

These strings are the only thing an arriving model reads before deciding
whether a document answered its question, so a wrong count or a mangled word
is not cosmetic: "3 of 3 sources" followed by "and 2 more" leaves the reader
unable to tell which number to believe.
"""

from uuid import uuid4

import pytest

from app.retrieval.format import (
    CitationIndex,
    format_context,
    navigation_hint,
    source_indices,
)
from app.retrieval.schemas import ContextSource


def source(*, heading: bool, ordinal: int = 0) -> ContextSource:
    return ContextSource(
        workspace_id=uuid4(),
        document_id=uuid4(),
        processing_run_id=uuid4(),
        chunk_id=uuid4(),
        ordinal=ordinal,
        score=0.9,
        content_hash="a" * 64,
        filename="report.pdf",
        section_path=["Part IV"],
        is_heading_only=heading,
        page_numbers=[1],
        text="text",
    )


def hint(sources: list[ContextSource]) -> str:
    indices = source_indices({source.chunk_id: source for source in sources})
    return navigation_hint(sources, indices)


def test_no_hint_when_every_source_carries_text() -> None:
    assert hint([source(heading=False), source(heading=False)]) == ""


def test_one_heading_among_several_reads_as_singular() -> None:
    text = hint([source(heading=True), source(heading=False), source(heading=False)])
    assert text.startswith("1 of 3 sources is a heading with no text of its own.")
    assert "this source is not evidence" in text


def test_every_heading_reads_as_plural() -> None:
    text = hint([source(heading=True), source(heading=True), source(heading=True)])
    assert text.startswith("3 of 3 sources are headings with no text of their own.")
    assert "these sources are not evidence" in text


def test_the_hint_never_contradicts_its_own_total() -> None:
    """The count already says how many there are; a trailing "and N more" fights it.

    The regression this covers read "3 of 3 sources are a heading ... and 2
    more", which states two different totals for the same list.
    """
    text = hint([source(heading=True) for _ in range(4)])
    assert "more" not in text


@pytest.mark.parametrize("count", [1, 2, 5])
def test_the_word_sources_is_never_garbled(count: int) -> None:
    """A string built by appending "s" to "sources" produced "sourcess"."""
    text = hint([source(heading=True) for _ in range(count)])
    assert "sourcess" not in text


def test_the_hint_names_the_first_heading_only() -> None:
    first = source(heading=True, ordinal=42)
    text = hint([first, source(heading=False)])
    assert "start_ordinal=42" in text
    assert f"document_id={first.document_id}" in text
    assert "source [1]" in text


def test_citation_index_reuses_a_chunk_for_the_whole_answer() -> None:
    """A passage served twice must keep one label, not gain a second."""
    first = source(heading=False)
    second = source(heading=False)
    index = CitationIndex()

    assert index.indices_for([first, second]) == {first.chunk_id: 1, second.chunk_id: 2}
    assert index.indices_for([first]) == {first.chunk_id: 1}

    third = source(heading=False)
    assert index.indices_for([third]) == {third.chunk_id: 3}


def test_the_source_label_names_what_a_reader_can_act_on() -> None:
    """A label carries the handles a tool takes, and nothing else.

    `chunk_id` was printed on every source and no tool accepted it: the chunk
    is reachable through the ordinal on the same line. Dropping it also takes
    a UUID off every label in every search.
    """
    first = source(heading=False, ordinal=3)
    body = format_context([first], source_indices({first.chunk_id: first}))
    label = body.splitlines()[0]

    assert f"ordinal={first.ordinal}" in label
    assert f"document_id={first.document_id}" in label
    assert f"workspace_id={first.workspace_id}" in label
    assert "page_number=1" in label
    assert "chunk_id" not in label
