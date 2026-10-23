"""The picture serializer: a picture in a chunk, and a marker for it.

Docling's own chunking serializer writes a picture as an empty string, so a
figure contributes nothing to the chunk it sits in and the text around it
reads as though the picture were never there.

This serializer writes a marker instead. The marker carries the picture's
own handle, which is what makes the rest of the stage possible: the chunk
knows which picture it holds, the description can be put back on the line
the picture was on, and a picture that is never described can still be
named.

The caption is written out too. When the parser attaches a caption to a
picture, that caption lives inside the picture rather than beside it, so a
serializer that wrote only a marker would drop the caption on the floor.
"""

from docling_core.transforms.serializer.base import (
    BaseDocSerializer,
    BasePictureSerializer,
    SerializationResult,
)
from docling_core.transforms.serializer.common import create_ser_result
from docling_core.types.doc import DoclingDocument
from docling_core.types.doc.document import PictureItem

from app.extraction.images import picture_marker


class RefPictureSerializer(BasePictureSerializer):
    """Writes a picture as its handle, plus its caption when it has one.

    ``marked_refs`` narrows which pictures get a marker. ``None`` marks every
    picture, which is the old behaviour. A set marks only the pictures the
    stage is going to describe, so a picture dropped by the size floor or the
    per-document cap keeps its caption but leaves no marker behind for the
    merge to turn into an ``[Image]`` line.
    """

    def __init__(self, *, marked_refs: set[str] | None = None) -> None:
        self._marked_refs = set(marked_refs) if marked_refs is not None else None

    def serialize(
        self,
        *,
        item: PictureItem,
        doc_serializer: BaseDocSerializer,
        doc: DoclingDocument,
        **kwargs: object,
    ) -> SerializationResult:
        parts: list[str] = []
        caption = doc_serializer.serialize_captions(item=item, **kwargs)
        if caption.text:
            parts.append(caption.text)
        if self._marked_refs is None or item.self_ref in self._marked_refs:
            parts.append(picture_marker(item.self_ref))
        return create_ser_result(text="\n\n".join(parts), span_source=item)
