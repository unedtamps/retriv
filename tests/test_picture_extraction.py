"""Collecting pictures out of a parsed document.

The captioned picture is the case that matters here. Docling keeps a
picture's caption as a reference into the document's text, not as the text
itself, so the collector has to resolve that reference. Using the reference
object as a dictionary key raises instead, and because the extractor wraps
every parser failure in one message, the whole document fails to index.
"""

import io
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("docling")
PIL_Image = pytest.importorskip("PIL.Image")

from docling_core.types.doc import (  # noqa: E402
    BoundingBox,
    CoordOrigin,
    DocItemLabel,
    DoclingDocument,
    ImageRef,
    ProvenanceItem,
    Size,
)

from app.documents.validation import PDF_MEDIA_TYPE  # noqa: E402
from app.extraction.docling import DoclingExtractor  # noqa: E402
from app.extraction.images import picture_marker  # noqa: E402


def an_extractor(**overrides: Any) -> DoclingExtractor:
    options: dict[str, Any] = {
        "ocr_languages": ["eng"],
        "chunk_max_tokens": 700,
        "tokenizer_name": "cl100k_base",
        "picture_images_enabled": True,
        "image_render_scale": 2.0,
        "image_min_pixels": 10_000,
        "max_images": 10,
    }
    options.update(overrides)
    return DoclingExtractor(**options)


def a_picture(
    doc: DoclingDocument,
    *,
    size: tuple[int, int] = (600, 300),
    caption: str = "",
) -> Any:
    picture = doc.add_picture(
        prov=ProvenanceItem(
            page_no=1,
            charspan=(0, 0),
            bbox=BoundingBox(l=0, t=0, r=100, b=100),
        )
    )
    picture.image = ImageRef.from_pil(PIL_Image.new("RGB", size), dpi=144)
    if caption:
        caption_item = doc.add_text(label=DocItemLabel.CAPTION, text=caption)
        picture.captions.append(caption_item.get_ref())
    return picture


def test_a_captioned_picture_keeps_its_caption() -> None:
    """The caption is a reference, and the text it names is the caption."""
    doc = DoclingDocument(name="report")
    a_picture(doc, caption="Figure 1: Net revenue by segment.")

    images = an_extractor()._collect_images(doc, None, PDF_MEDIA_TYPE)

    assert len(images) == 1
    assert images[0].caption == "Figure 1: Net revenue by segment."


def test_a_picture_dropped_for_its_size_keeps_its_caption_but_gets_no_marker() -> None:
    """A logo is not worth a description, but it is still a word on the page.

    Only the pictures that will be described get markers. A dropped picture
    keeps whatever caption the parser attached to it, so a caption is never
    lost, and it leaves no marker behind for the merge to turn into an
    ``[Image]`` line.
    """
    doc = DoclingDocument(name="report")
    kept = a_picture(doc, caption="Figure 1: Net revenue by segment.")
    dropped = a_picture(doc, size=(50, 50), caption="Figure 2: Company logo.")
    doc.add_text(label=DocItemLabel.TEXT, text="Revenue grew across all segments.")

    extractor = an_extractor()
    images = extractor._collect_images(doc, None, PDF_MEDIA_TYPE)
    extractor._serializer_provider.mark_pictures({image.ref for image in images})
    text = "\n".join(chunk.text for chunk in extractor._chunker.chunk(doc))

    assert picture_marker(kept.self_ref) in text
    assert picture_marker(dropped.self_ref) not in text
    assert "Figure 2: Company logo." in text


def a_pdf_with_a_marked_region(
    tmp_path: Path,
) -> Path:
    """A one-page PDF with a solid blue rectangle at a known place."""
    pymupdf = pytest.importorskip("pymupdf")

    pdf = pymupdf.open()
    page = pdf.new_page(width=612, height=792)
    page.draw_rect(pymupdf.Rect(100, 200, 300, 400), fill=(0, 0, 1))
    pdf_path = tmp_path / "drawing.pdf"
    pdf_path.write_bytes(pdf.tobytes())
    pdf.close()
    return pdf_path


def a_bbox_over_the_marked_region(origin: Any) -> BoundingBox:
    """The box over the rectangle, in the given coordinate origin.

    The rectangle spans x 100-300 and, from the top edge, y 200-400 of a
    792-point page, which in a bottom-left frame is t = 592 and b = 392.
    """
    if origin == CoordOrigin.BOTTOMLEFT:
        return BoundingBox(l=100, t=592, r=300, b=392, coord_origin=CoordOrigin.BOTTOMLEFT)
    return BoundingBox(l=100, t=200, r=300, b=400, coord_origin=CoordOrigin.TOPLEFT)


def a_pdf_document_with_the_region(origin: Any) -> DoclingDocument:
    doc = DoclingDocument(name="drawing")
    doc.add_page(page_no=1, size=Size(width=612, height=792))
    doc.add_picture(
        prov=ProvenanceItem(page_no=1, charspan=(0, 0), bbox=a_bbox_over_the_marked_region(origin))
    )
    return doc


def test_a_pdf_picture_without_parser_bytes_is_cropped_from_the_source(
    tmp_path: Path,
) -> None:
    """A parsed PDF carries no picture bytes; the crop comes from the file.

    With the cropping moved out of the parser, a PDF picture reaches the
    collector as a bbox on a page and nothing else. The rendered crop has to
    land where the bbox says -- which is what the bottom-left to top-left
    flip exists for -- and come back as a PNG the model can look at.
    """
    pdf_path = a_pdf_with_a_marked_region(tmp_path)
    doc = a_pdf_document_with_the_region(CoordOrigin.BOTTOMLEFT)

    images = an_extractor()._collect_images(doc, pdf_path, PDF_MEDIA_TYPE)

    assert len(images) == 1
    assert images[0].media_type == "image/png"
    assert (images[0].width, images[0].height) == (400, 400)
    render = PIL_Image.open(io.BytesIO(images[0].data))
    assert render.getpixel((200, 200)) == (0, 0, 255)


def test_a_top_left_bbox_is_cropped_without_a_flip(tmp_path: Path) -> None:
    """A bbox that already says which origin it is in needs no translation."""
    pdf_path = a_pdf_with_a_marked_region(tmp_path)
    doc = a_pdf_document_with_the_region(CoordOrigin.TOPLEFT)

    images = an_extractor()._collect_images(doc, pdf_path, PDF_MEDIA_TYPE)

    assert len(images) == 1
    assert (images[0].width, images[0].height) == (400, 400)
    render = PIL_Image.open(io.BytesIO(images[0].data))
    assert render.getpixel((200, 200)) == (0, 0, 255)


def test_a_pdf_picture_below_the_pixel_floor_is_never_rendered(
    tmp_path: Path,
) -> None:
    """The floor check runs before the render, so a logo costs no pixmap."""
    pymupdf = pytest.importorskip("pymupdf")

    pdf = pymupdf.open()
    page = pdf.new_page(width=612, height=792)
    page.draw_rect(pymupdf.Rect(100, 100, 110, 110), fill=(0, 0, 1))
    pdf_path = tmp_path / "drawing.pdf"
    pdf_path.write_bytes(pdf.tobytes())
    pdf.close()
    doc = DoclingDocument(name="drawing")
    doc.add_page(page_no=1, size=Size(width=612, height=792))
    doc.add_picture(
        prov=ProvenanceItem(
            page_no=1,
            charspan=(0, 0),
            bbox=BoundingBox(l=100, t=692, r=110, b=682, coord_origin=CoordOrigin.BOTTOMLEFT),
        )
    )

    images = an_extractor()._collect_images(doc, pdf_path, PDF_MEDIA_TYPE)

    assert images == []


def test_the_byte_caps_bound_collection_without_losing_the_rest(
    tmp_path: Path,
) -> None:
    """The per-image cap skips one picture, the total cap stops the collector.

    Both caps used to run when the sandbox wrote the files, after every
    picture had been decoded into memory. They run in the collector now, so
    they have to keep the same shape: an oversized picture is dropped and
    the collector goes on, a full budget stops it and keeps what it gathered.
    """
    pdf_path = a_pdf_with_a_marked_region(tmp_path)
    doc = a_pdf_document_with_the_region(CoordOrigin.BOTTOMLEFT)
    doc.add_picture(
        prov=ProvenanceItem(
            page_no=1,
            charspan=(0, 0),
            bbox=a_bbox_over_the_marked_region(CoordOrigin.BOTTOMLEFT),
        )
    )
    extractor = an_extractor()
    one_picture_bytes = len(extractor._collect_images(doc, pdf_path, PDF_MEDIA_TYPE)[0].data)

    skipped = an_extractor(max_image_bytes=one_picture_bytes - 1)._collect_images(
        doc, pdf_path, PDF_MEDIA_TYPE
    )
    stopped = an_extractor(max_total_image_bytes=2 * one_picture_bytes - 1)._collect_images(
        doc, pdf_path, PDF_MEDIA_TYPE
    )

    assert skipped == []
    assert len(stopped) == 1
