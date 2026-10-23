"""Tables, serialized one grid row per line so a table can be split on rows.

Docling reads a table as a grid, and the chunker turns that grid into a
single line of text: ``row, column = value`` for every cell, all run
together. The format is good -- a cell's row label and column label travel
with its value -- but it is one line, so when a table is longer than the chunk
budget the splitter has no boundary to cut on and cuts wherever the tokens
happen to fall. The cut lands inside a cell: on this corpus, half the table
chunks in the 10-K and seven of the eight in the UNDP regulations were cut
that way, and the chunks end with fragments like ``Page No. =`` or ``and
Reg``.

A table is not a paragraph. Its rows are the natural unit, and a row is the
unit a reader thinks in. So the table is serialized one row per line, and the
chunker's line-based splitter -- already there, and never cuts a line unless
that line alone exceeds the budget -- can cut on row boundaries instead.

The header is repeated in every part. A part of a table that does not say
which columns it holds is a table of anonymous numbers, and the header is
what makes the part readable on its own.

Nothing here decides what a table *means*. The column labels are the ones
Docling's own dataframe carries, so a table whose header Docling did not
detect keeps the positional labels it has today rather than being given a
header this module guessed at. The same goes for the row label: the first
column is taken as the row label because that is what the format Docling
produces does, and a table whose first column is really a data column keeps
that reading rather than being corrected here.
"""

from typing import Any

from docling_core.transforms.chunker.hierarchical_chunker import (
    ChunkingDocSerializer,
    ChunkingSerializerProvider,
)
from docling_core.transforms.serializer.base import (
    BaseDocSerializer,
    BasePictureSerializer,
    BaseTableSerializer,
    SerializationResult,
)
from docling_core.transforms.serializer.common import create_ser_result
from docling_core.transforms.serializer.markdown import MarkdownPictureSerializer
from docling_core.types.doc import DoclingDocument, TableItem

from app.extraction.pictures import RefPictureSerializer

HEADER_SEPARATOR = " | "
"""Between the cells of the header line. A body line never contains it."""

VALUE_SEPARATOR = " = "
"""Between a cell's label and its value. Only a body line contains it."""


class RowWiseTableSerializer(BaseTableSerializer):
    """Serialize a table as one line per grid row, under a pinned header line.

    The header line is the one line of the table that says what the columns
    are. The chunker looks for it again in the text it is about to split, and
    repeats it at the top of every part, so a part that was cut out of the
    middle of a table still says which columns it holds.
    """

    def serialize(
        self,
        *,
        item: TableItem,
        doc_serializer: BaseDocSerializer,
        doc: DoclingDocument,
        **kwargs: Any,
    ) -> SerializationResult:
        parts: list[SerializationResult] = []
        shared_visited = kwargs.get("visited")

        caption = doc_serializer.serialize_captions(item=item, **kwargs)
        if caption.text:
            parts.append(caption)

        if item.self_ref not in doc_serializer.get_excluded_refs(**kwargs):
            local_kwargs = (
                {**kwargs, "visited": set(shared_visited)}
                if shared_visited is not None
                else kwargs
            )
            table_text = self._table_text(item, doc)
            if table_text:
                if shared_visited is not None:
                    shared_visited.update(local_kwargs["visited"])
                parts.append(create_ser_result(text=table_text, span_source=item))

        return create_ser_result(
            text="\n\n".join(part.text for part in parts), span_source=parts
        )

    def get_header_and_body_lines(
        self, *, table_text: str, **kwargs: Any
    ) -> tuple[list[str], list[str]]:
        """Split the chunk text into the header line and the lines after it.

        The chunker hands over the whole chunk, which for a table is its
        caption followed by the table. The caption is not part of the body
        lines: the chunker treats whatever precedes the header as a preamble
        and keeps it in the first part only, which is where a caption belongs.

        The header is found by its shape rather than by remembering what this
        serializer wrote. A header line holds the column names separated by
        ``|`` and no cell value; a body line holds ``=`` because every row
        line is a list of labelled cells. The line after the header must be a
        body line, which is what keeps a caption that happens to contain a
        pipe from being mistaken for the header.
        """
        lines = table_text.splitlines(True)
        for index, line in enumerate(lines[:-1]):
            if (
                HEADER_SEPARATOR in line
                and VALUE_SEPARATOR not in line
                and VALUE_SEPARATOR in lines[index + 1]
            ):
                return [line], lines[index + 1 :]
        return [], lines

    def _table_text(self, item: TableItem, doc: DoclingDocument) -> str:
        """Render the table as a header line followed by one line per row."""
        frame = item.export_to_dataframe(doc)
        if frame.empty or frame.shape[1] == 0:
            return ""

        width = frame.shape[1]
        columns = [str(column) for column in frame.columns]
        # Docling names the columns 0..n-1 when it found no header row and
        # names them after the header when it did, so the two are told apart by
        # the names themselves. A table with no header keeps the positional
        # labels it has today: guessing a header here would put labels on
        # values.
        has_header = columns != [str(index) for index in range(width)]

        if width == 1:
            # Nothing to pair a label with, so every row is its own line.
            values = [str(value).strip() for value in frame.iloc[:, 0]]
            single = [value for value in values if value]
            if not has_header:
                return "\n".join(single)
            return "\n".join([values[0], *single]) if values[0] else "\n".join(single)

        if has_header:
            header_line = HEADER_SEPARATOR.join(columns)
            labels = columns[1:]
        else:
            header_line = ""
            labels = [str(index) for index in range(1, width)]

        body: list[str] = []
        for row in range(frame.shape[0]):
            row_label = str(frame.iloc[row, 0]).strip()
            cells: list[str] = []
            for column in range(1, width):
                value = str(frame.iloc[row, column]).strip()
                if not value:
                    continue
                label = labels[column - 1]
                if row_label:
                    cells.append(f"{row_label}, {label} = {value}")
                else:
                    cells.append(f"{label} = {value}")
            if cells:
                body.append(". ".join(cells))

        if not body:
            return header_line
        if not header_line:
            return "\n".join(body)
        return "\n".join([header_line, *body])


class RowWiseChunkingSerializer(ChunkingDocSerializer):
    """The chunking serializer, with tables serialized one row per line.

    The picture serializer is set here too, because this is the serializer
    the pipeline chunks with: a picture is written as a marker naming it, so
    the chunk knows which picture it holds and the description can be put
    back on the line the picture was on. Docling's own serializer writes a
    picture as an empty string, which drops it from the chunk entirely.
    """

    table_serializer: BaseTableSerializer = RowWiseTableSerializer()
    picture_serializer: BasePictureSerializer = RefPictureSerializer()


class UnmarkedChunkingSerializer(RowWiseChunkingSerializer):
    """The chunking serializer for when the picture stage is off.

    A picture is written as an empty string, exactly as Docling writes it,
    so the picture contributes nothing to the chunk and the document is
    indexed the way it was before the stage existed. A marker would be
    wrong here: nothing would ever come and replace it, and it would be
    embedded into the document as leftover syntax.
    """

    picture_serializer: BasePictureSerializer = MarkdownPictureSerializer()


class RowWiseSerializerProvider(ChunkingSerializerProvider):
    """Hands the chunker a serializer that writes tables one row per line.

    ``mark_pictures`` decides how pictures are written. Given the pictures
    that will be described, those are written as markers naming them and the
    rest are written as caption text only. Given none, every picture is
    written as nothing at all. Neither is known until the document has been
    parsed, which is why the provider is stateful.
    """

    def __init__(self, *, pictures_marked: bool = True) -> None:
        self._pictures_marked = pictures_marked
        self._marked_refs: set[str] | None = None

    def mark_pictures(self, refs: set[str]) -> None:
        """Mark exactly these pictures; leave the others as captions only."""
        self._marked_refs = set(refs)
        self._pictures_marked = bool(refs)

    def get_serializer(self, doc: DoclingDocument) -> BaseDocSerializer:
        if self._pictures_marked:
            return RowWiseChunkingSerializer(
                doc=doc,
                picture_serializer=RefPictureSerializer(
                    marked_refs=self._marked_refs
                ),
            )
        return UnmarkedChunkingSerializer(doc=doc)
