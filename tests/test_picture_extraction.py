"""Collecting pictures out of a parsed document.

The captioned picture is the case that matters here. Docling keeps a
picture's caption as a reference into the document's text, not as the text
itself, so the collector has to resolve that reference. Using the reference
object as a dictionary key raises instead, and because the extractor wraps
every parser failure in one message, the whole document fails to index.
"""

from typing import Any

import pytest

pytest.importorskip("docling")
PIL_Image = pytest.importorskip("PIL.Image")

from docling_core.types.doc import (  # noqa: E402
    BoundingBox,
    DocItemLabel,
    DoclingDocument,
    ImageRef,
    ProvenanceItem,
)

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

    images = an_extractor()._collect_images(doc)

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
    images = extractor._collect_images(doc)
    extractor._serializer_provider.mark_pictures({image.ref for image in images})
    text = "\n".join(chunk.text for chunk in extractor._chunker.chunk(doc))

    assert picture_marker(kept.self_ref) in text
    assert picture_marker(dropped.self_ref) not in text
    assert "Figure 2: Company logo." in text
