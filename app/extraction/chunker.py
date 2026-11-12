"""Packing the block stream into the chunks the pipeline stores.

The chunker owns the three guarantees the rest of the pipeline leans on:
every block lands in exactly one chunk, the chunks come out in reading
order -- their list position becomes the stored ordinal -- and each chunk
carries the section path its blocks sat under and the pages its blocks sit
on. The section path is advisory labelling; the order and the pages are the
contract.

Content is packed one line at a time under a token budget. A paragraph is
one line; a table is a header line followed by one line per row, so a table
longer than the budget splits at a row boundary and the header is repeated
at the top of every part -- a part that does not say which columns it holds
is a table of anonymous numbers. A single line longer than the budget is
cut at a token boundary, which is the only place a cut can land.

A heading followed by content is not a chunk of its own: its text is
prepended to the content's chunks, so what the model reads and embeds
carries the section it belongs to. A heading followed by another heading is
emitted alone -- that is the chunk a search can land on to tell the reader
where the next section starts.
"""

import tiktoken

from app.extraction.base import ExtractedChunk
from app.extraction.images import picture_marker
from app.extraction.markdown_ir import DocumentBlock, SourceRegion
from app.extraction.tables import (
    markdown_table_cells,
    row_wise_table_text,
    split_header_and_body,
)
from app.sections import encode_section_key


class _Line:
    """One packable line: its text, page, table header, and source regions."""

    __slots__ = ("text", "page_no", "table_header", "regions")

    def __init__(
        self,
        text: str,
        page_no: int,
        table_header: str | None = None,
        regions: tuple[SourceRegion, ...] = (),
    ) -> None:
        self.text = text
        self.page_no = page_no
        self.table_header = table_header
        self.regions = regions


def _line_regions(
    lines: list[_Line],
) -> tuple[list[dict[str, object]], list[str]]:
    """Unique source regions and image refs across a chunk's lines."""
    regions: list[dict[str, object]] = []
    source_refs: list[str] = []
    seen: set[
        tuple[
            int | None,
            tuple[float, float, float, float] | None,
            str,
            str,
            str,
        ]
    ] = set()
    seen_refs: set[str] = set()
    for line in lines:
        for region in line.regions:
            key = (
                region.page_no,
                region.bbox,
                region.kind,
                region.ref,
                region.anchor,
            )
            if key in seen:
                continue
            seen.add(key)
            regions.append(
                {
                    "page_no": region.page_no,
                    "bbox": list(region.bbox) if region.bbox is not None else None,
                    "kind": region.kind,
                    "ref": region.ref,
                    "anchor": region.anchor,
                }
            )
            if region.ref and region.ref not in seen_refs:
                seen_refs.add(region.ref)
                source_refs.append(region.ref)
    return regions, source_refs


class MarkdownChunker:
    def __init__(self, *, chunk_max_tokens: int, tokenizer_name: str) -> None:
        self._max_tokens = chunk_max_tokens
        self._encoding = tiktoken.get_encoding(tokenizer_name)

    def count_tokens(self, text: str) -> int:
        return len(self._encoding.encode(text))

    def chunk(self, blocks: list[DocumentBlock]) -> list[ExtractedChunk]:
        chunks: list[ExtractedChunk] = []
        stack: list[tuple[int, str]] = []
        open_heading: DocumentBlock | None = None
        lines: list[_Line] = []
        section_path: list[str] = []

        for block in blocks:
            if block.kind == "heading":
                if open_heading is not None:
                    self._emit_heading(chunks, open_heading, stack)
                self._flush(chunks, lines, section_path)
                while stack and stack[-1][0] >= block.level:
                    stack.pop()
                stack.append((block.level, block.text))
                open_heading = block
                lines = []
                section_path = []
                continue
            if not lines:
                section_path = [text for _, text in stack]
                open_heading = None
            self._pack(chunks, lines, section_path, block)
        if open_heading is not None:
            self._emit_heading(chunks, open_heading, stack)
        self._flush(chunks, lines, section_path)
        return chunks

    def _pack(
        self,
        chunks: list[ExtractedChunk],
        lines: list[_Line],
        section_path: list[str],
        block: DocumentBlock,
    ) -> None:
        for line in self._block_lines(block):
            if lines and self._content_tokens(lines, section_path) + self.count_tokens(
                line.text
            ) > self._max_tokens:
                self._flush(chunks, lines, section_path)
            if not lines and line.table_header:
                lines.append(
                    _Line(
                        line.table_header,
                        line.page_no,
                        regions=line.regions,
                    )
                )
            lines.append(line)

    def _block_lines(self, block: DocumentBlock) -> list[_Line]:
        if block.kind == "image":
            marker = picture_marker(block.ref) if block.ref else block.text
            return [_Line(marker, block.page_no, regions=block.regions)]
        if block.kind == "table":
            return self._table_lines(block)
        line = " ".join(block.text.split())
        if self.count_tokens(line) <= self._max_tokens:
            return [_Line(line, block.page_no, regions=block.regions)]
        return self._token_split(line, block.page_no, block.regions)

    def _token_split(
        self,
        line: str,
        page_no: int,
        regions: tuple[SourceRegion, ...],
    ) -> list[_Line]:
        """A single line longer than the budget, cut at token boundaries.

        The cut has no natural place to land -- the line is one sentence or
        one very long paragraph -- so the pieces are re-packed as ordinary
        lines and become chunks of their own.
        """
        tokens = self._encoding.encode(line)
        return [
            _Line(
                self._encoding.decode(tokens[start : start + self._max_tokens]),
                page_no,
                regions=regions,
            )
            for start in range(0, len(tokens), self._max_tokens)
        ]

    def _table_lines(self, block: DocumentBlock) -> list[_Line]:
        cells = markdown_table_cells(block.text)
        if cells is None:
            serialized_header, serialized_rows = split_header_and_body(block.text)
            if not serialized_header:
                return [
                    _Line(
                        " ".join(block.text.split()),
                        block.page_no,
                        regions=block.regions,
                    )
                ]
            return [
                _Line(
                    serialized_header[0],
                    block.page_no,
                    regions=block.regions,
                ),
                *(
                    _Line(
                        row,
                        block.page_no,
                        table_header=serialized_header[0],
                        regions=block.regions,
                    )
                    for row in serialized_rows
                ),
            ]
        header, rows = cells
        if len(header) < 2:
            values = [header[0], *(row[0] for row in rows)] if header else []
            return [
                _Line(text, block.page_no, regions=block.regions)
                for text in (value for value in values if value)
            ]
        header_line = " | ".join(header)
        lines = [
            _Line(header_line, block.page_no, regions=block.regions)
        ]
        for row in row_wise_table_text(header, rows, has_header=True).splitlines()[1:]:
            lines.append(
                _Line(
                    row,
                    block.page_no,
                    table_header=header_line,
                    regions=block.regions,
                )
            )
        return lines

    def _content_tokens(self, lines: list[_Line], section_path: list[str]) -> int:
        return self.count_tokens(self._content_text(lines, section_path))

    def _content_text(self, lines: list[_Line], section_path: list[str]) -> str:
        prefix = " > ".join(section_path) + "\n\n" if section_path else ""
        return prefix + "\n".join(line.text for line in lines)

    def _flush(
        self,
        chunks: list[ExtractedChunk],
        lines: list[_Line],
        section_path: list[str],
    ) -> None:
        if not lines:
            return
        text = self._content_text(lines, section_path)
        pages = sorted({line.page_no for line in lines if line.page_no > 0})
        regions, source_refs = _line_regions(lines)
        chunks.append(
            ExtractedChunk(
                text=text,
                token_count=self.count_tokens(text),
                section_path=section_path,
                section_key=encode_section_key(section_path),
                is_heading_only=False,
                page_numbers=pages,
                provenance={
                    "source_refs": source_refs,
                    "page_regions": [{"page_no": page} for page in pages],
                    "regions": regions,
                },
            )
        )
        lines.clear()

    def _emit_heading(
        self,
        chunks: list[ExtractedChunk],
        block: DocumentBlock,
        stack: list[tuple[int, str]],
    ) -> None:
        section_path = [text for _, text in stack]
        text = " > ".join(section_path) if section_path else block.text
        page = block.page_no
        regions = [
            {
                "page_no": region.page_no,
                "bbox": list(region.bbox) if region.bbox is not None else None,
                "kind": region.kind,
                "ref": region.ref,
                "anchor": region.anchor,
            }
            for region in block.regions
        ]
        source_refs = [region.ref for region in block.regions if region.ref]
        chunks.append(
            ExtractedChunk(
                text=text,
                token_count=self.count_tokens(text),
                section_path=section_path,
                section_key=encode_section_key(section_path),
                is_heading_only=True,
                page_numbers=[page] if page > 0 else [],
                provenance={
                    "source_refs": source_refs,
                    "page_regions": [{"page_no": page}] if page > 0 else [],
                    "regions": regions,
                },
            )
        )
