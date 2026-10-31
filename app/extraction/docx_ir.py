"""The native markdown intermediate representation for DOCX documents.

A DOCX carries its structure in styles, so the walk is deterministic: a
paragraph set in a heading style becomes a heading, a table becomes rows,
an embedded picture becomes a marker. The document has no pages, so no
page numbers exist here -- the same is true of how the old parser reported
DOCX, and every consumer already treats an empty page list as "pages
unknown".
"""

from dataclasses import replace
from pathlib import Path
from typing import Any

import docx
from docx.table import Table
from docx.text.paragraph import Paragraph

from app.extraction.base import ExtractedImage
from app.extraction.images import fit_image_to_limit, picture_marker
from app.extraction.markdown_ir import DocumentBlock
from app.extraction.tables import row_wise_table_text

_DOCX_IMAGE_MAX_EDGE = 1568
"""Longest side worth carrying into the pipeline. A model downscales past
this itself, so a bigger bitmap buys precision nobody reads."""

_HEADING_STYLES = {"title": 1, "subtitle": 2}


def build_docx_blocks(
    path: Path,
    *,
    image_render_scale: float,
    max_images: int = 0,
    max_image_bytes: int = 0,
    max_total_image_bytes: int = 0,
) -> tuple[list[DocumentBlock], list[ExtractedImage]]:
    """The document as blocks and pictures, in the order it is written.

    ``image_render_scale`` is accepted but unused: a DOCX picture arrives
    with its own pixels, so there is nothing to scale. It keeps the
    extractor's two format paths symmetric for the caller. The byte and
    count caps mean what they mean in the PDF path: a picture over the
    per-image cap is skipped, and collection stops at the total -- the cap
    applies where the bytes come into existence, so no marker is ever
    written for a picture that was never shipped.
    """
    document = docx.Document(str(path))
    blocks: list[DocumentBlock] = []
    images: list[ExtractedImage] = []
    total_bytes = 0

    def take(candidates: list[ExtractedImage]) -> None:
        nonlocal total_bytes
        for image in candidates:
            if max_images and len(images) >= max_images:
                return
            fitted = _bounded_image(image, max_image_bytes)
            if fitted is None:
                continue
            if max_total_image_bytes and total_bytes + len(fitted.data) > max_total_image_bytes:
                return
            total_bytes += len(fitted.data)
            images.append(fitted)
            blocks.append(_image_block(fitted))

    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            paragraph = Paragraph(child, document)
            blocks.extend(_paragraph_blocks(paragraph))
            blocks.extend(_textbox_blocks(paragraph, document))
            take(_element_images(paragraph._p, document, len(images)))
        elif child.tag.endswith("}tbl"):
            table = Table(child, document)
            text = _table_text(table)
            if text:
                blocks.append(DocumentBlock(kind="table", text=text, page_no=0))
            take(_element_images(table._tbl, document, len(images)))
    return blocks, images


def _bounded_image(image: ExtractedImage, max_bytes: int) -> ExtractedImage | None:
    """The picture shrunk under the byte cap, or ``None`` if it cannot be.

    The cap exists to keep an oversized picture out of the pipeline, not to
    throw its content away: a 33 MB camera photo says the same thing at 800 KB.
    """
    if not max_bytes or len(image.data) <= max_bytes:
        return image
    fitted = fit_image_to_limit(
        image.data, image.media_type, max_bytes, _DOCX_IMAGE_MAX_EDGE
    )
    if fitted is None:
        return None
    data, media_type = fitted
    return replace(image, data=data, media_type=media_type)


def _image_block(image: ExtractedImage) -> DocumentBlock:
    return DocumentBlock(kind="image", text=picture_marker(image.ref), page_no=0)


def _textbox_blocks(paragraph: Paragraph, document: Any) -> list[DocumentBlock]:
    """The words a text box anchored to this paragraph carries.

    Sidebars and callouts live in ``w:txbxContent`` inside a run, which the
    body walk never visits. Each box's children are read like a body --
    paragraphs with their styles, tables as rows -- and emitted after the
    paragraph that anchors them. A box nested in a box matches its own
    ``w:txbxContent`` too, so nothing is read twice: only direct children
    count for each.
    """
    blocks: list[DocumentBlock] = []
    for content in paragraph._p.xpath(".//w:txbxContent"):
        for child in content.iterchildren():
            if child.tag.endswith("}p"):
                blocks.extend(_paragraph_blocks(Paragraph(child, document)))
            elif child.tag.endswith("}tbl"):
                text = _table_text(Table(child, document))
                if text:
                    blocks.append(DocumentBlock(kind="table", text=text, page_no=0))
    return blocks


def _paragraph_blocks(paragraph: Paragraph) -> list[DocumentBlock]:
    text = " ".join(paragraph.text.split())
    if not text:
        return []
    level = _heading_level(paragraph)
    if level is not None:
        return [DocumentBlock(kind="heading", text=text, page_no=0, level=level)]
    return [DocumentBlock(kind="text", text=text, page_no=0)]


def _heading_level(paragraph: Paragraph) -> int | None:
    try:
        style = paragraph.style
        name = (style.name if style is not None else "") or ""
    except Exception:
        return None
    lowered = name.strip().lower()
    if lowered in _HEADING_STYLES:
        return _HEADING_STYLES[lowered]
    if lowered.startswith("heading"):
        suffix = lowered.removeprefix("heading").strip()
        if suffix.isdigit() and 1 <= int(suffix) <= 6:
            return int(suffix)
    return None


def _table_text(table: Table) -> str:
    grid = [
        [" ".join(cell.text.split()) for cell in row.cells] for row in table.rows
    ]
    if not grid or not any(any(cell for cell in row) for row in grid):
        return ""
    header, rows = grid[0], grid[1:]
    return row_wise_table_text(header, rows, has_header=True)


def _element_images(
    element: Any, document: Any, count: int
) -> list[ExtractedImage]:
    """The pictures embedded in one body element, as their stored bytes.

    A picture inside a table cell lives in a paragraph the body walk never
    visits, so the table element is scanned for the same embed references --
    the scan reaches every descendant paragraph, nested tables included.
    """
    blips = element.xpath(".//a:blip/@r:embed")
    images: list[ExtractedImage] = []
    for r_id in blips:
        part = document.part.related_parts.get(r_id)
        if part is None:
            continue
        data = part.blob
        if not data:
            continue
        images.append(
            ExtractedImage(
                ref=f"image-docx-{count + len(images)}",
                page_number=0,
                media_type=part.content_type or "image/png",
                data=data,
            )
        )
    return images
