"""Putting a picture's description back where the picture was.

The parser writes a marker into the chunk text where a picture stood, and
this module is what takes the marker out again once the picture has been
described. The marker carries the parser's own handle for the picture, so the
description lands on the line the picture was on, in the section the picture
was in, between the sentences that talk about it.

The description is the model's words, not the document's, so it is labelled
as such. A reader that finds a chart's values in a document that never
printed them should be able to see that they came from a description rather
than from the source.
"""

import re
import struct
from dataclasses import replace

from app.extraction.base import ExtractedChunk, ExtractedImage

PICTURE_MARKER_PATTERN = re.compile(r"\[\[PICTURE:([^\[\]]+)\]\]")
"""Matches the marker the parser leaves behind for a picture."""

IMAGE_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/tiff": ".tiff",
}
"""File suffix for each media type the extractors produce, shared by the
sandbox writer and the worker's artifact persistence."""

IMAGE_SIGNATURES: tuple[tuple[bytes, bytes], ...] = (
    (b"\xff\xd8\xff", b"jpeg"),
    (b"\x89PNG\r\n\x1a\n", b"png"),
    (b"GIF8", b"gif"),
    (b"II*\x00", b"tiff"),
    (b"MM\x00*", b"tiff"),
)

LOSSY_IMAGE_FORMATS = frozenset({b"jpeg"})
"""Formats that stay lossy when re-encoded. PNG, GIF and TIFF stay lossless:
they are what charts and screenshots arrive as, and a lossy generation blurs
exactly the small text a reader needs."""

JPEG_REENCODE_QUALITY = 85
"""Quality for a re-encoded JPEG. Visually indistinguishable from the source
and several times smaller than storing photographic pixels exactly."""


def image_format_of(data: bytes) -> bytes | None:
    """The picture's own format, read from its signature bytes."""
    for signature, name in IMAGE_SIGNATURES:
        if data.startswith(signature):
            return name
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return b"webp"
    return None


def image_pixel_size(data: bytes, image_format: bytes) -> tuple[int, int] | None:
    """The bitmap's width and height in pixels, straight from its header.

    pymupdf answers in points scaled by the embedded DPI, which is off by the
    ratio between the two; the header carries the real pixel count.
    """
    if image_format == b"png" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return (width, height) if width and height else None
    if image_format == b"gif" and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return (width, height) if width and height else None
    if image_format == b"jpeg":
        return _jpeg_pixel_size(data)
    return None


def _jpeg_pixel_size(data: bytes) -> tuple[int, int] | None:
    """Walk the JPEG marker chain to the frame header, which holds the size."""
    index = 2
    length = len(data)
    while index + 9 < length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        segment_length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        # SOF0..SOF15, excluding the non-frame markers DHT/JPG/DAC.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return (width, height) if width and height else None
        index += 2 + segment_length
    return None


def fit_image_to_limit(
    data: bytes, media_type: str, max_bytes: int, max_edge: int
) -> tuple[bytes, str] | None:
    """The picture re-encoded under a byte cap, or ``None`` when it cannot be.

    A picture is bounded by what a model can read, not by how large the file
    happened to be: a 33 MB camera photo carries the same content as an 800 KB
    render of it. So an oversized picture is shrunk -- in steps, keeping its
    own format -- rather than dropped, which is what leaving the cap to the
    collector would do: silently, with no marker and no row.

    ``max_edge`` bounds the first attempt, because pixels are what a model
    reads; the byte cap is then met by tightening quality. A picture that is
    already inside both limits is returned untouched, so nothing is
    decoded and re-encoded for no reason.
    """
    import pymupdf

    image_format = image_format_of(data)
    if image_format is None:
        return (data, media_type) if len(data) <= max_bytes else None
    size = image_pixel_size(data, image_format)
    if size is None:
        return (data, media_type) if len(data) <= max_bytes else None

    long_edge = max(size)
    if len(data) <= max_bytes and long_edge <= max_edge:
        return data, media_type

    try:
        source = pymupdf.open(stream=data)
    except Exception:
        return (data, media_type) if len(data) <= max_bytes else None
    try:
        page = source[0]
        edge = min(long_edge, max_edge)
        # Shrink by pixels first, then tighten quality; a camera photo loses
        # nothing a model reads at these sizes.
        for _attempt in range(6):
            factor = edge / long_edge
            scale_x = size[0] * factor / max(page.rect.width, 1e-6)
            scale_y = size[1] * factor / max(page.rect.height, 1e-6)
            rendered = page.get_pixmap(
                matrix=pymupdf.Matrix(scale_x, scale_y), alpha=False
            )
            if image_format in LOSSY_IMAGE_FORMATS:
                for quality in (JPEG_REENCODE_QUALITY, 70, 55, 40):
                    candidate = rendered.tobytes("jpeg", jpg_quality=quality)
                    if len(candidate) <= max_bytes:
                        return candidate, "image/jpeg"
            else:
                candidate = rendered.tobytes("png")
                if len(candidate) <= max_bytes:
                    return candidate, "image/png"
            if edge <= 320:
                break
            edge = max(320, edge * 3 // 4)
        return None
    except Exception:
        return (data, media_type) if len(data) <= max_bytes else None
    finally:
        source.close()


DESCRIPTION_PREFIX = "[Image description: "
DESCRIPTION_SUFFIX = "]"
"""How a described picture reads in the chunk text."""

UNDESCRIBED_PICTURE = "[Image]"
"""What a picture reads as when it was never described.

A chunk that held nothing but a picture would be empty without it, and an
empty chunk is a permanent processing error. The marker says less than the
description would, but it keeps the chunk honest and the pipeline moving.
"""


def picture_marker(ref: str) -> str:
    """The marker that stands in for a picture in the chunk text."""
    return f"[[PICTURE:{ref}]]"


def picture_refs(text: str) -> list[str]:
    """The handles of the pictures a chunk text holds, in order."""
    return PICTURE_MARKER_PATTERN.findall(text)


def count_tokens(text: str, tokenizer_name: str) -> int:
    """Count the tokens in a piece of text.

    The tokenizer is imported here rather than at the top of the module
    because this module is also loaded by the API process, which has no
    extraction stack and therefore no tokenizer. The API never calls this;
    the worker does, and the worker image has it.
    """
    import tiktoken

    return len(tiktoken.get_encoding(tokenizer_name).encode(text))


NO_FIGURE_REPLY = "none"
"""The reply a vision model gives for a scanned page with no figure on it."""


def is_no_figure_reply(description: str) -> bool:
    """Whether a scanned-page reply says the page holds nothing to describe."""
    return description.strip().rstrip(".!").lower() == NO_FIGURE_REPLY


def merge_descriptions(
    chunks: list[ExtractedChunk],
    descriptions: dict[str, str],
    *,
    images: list[ExtractedImage],
    tokenizer_name: str,
    model: str,
) -> list[ExtractedChunk]:
    """Replace every picture marker with the description of that picture.

    ``descriptions`` maps a picture's handle to its description. A picture
    with no entry was never described -- the document had more pictures than
    the stage was allowed to describe -- and its marker becomes
    ``[Image]`` rather than a line of leftover syntax. A lifted figure keeps
    that fallback; a scanned page offered for judgement does not: most scan
    pages hold no figure at all, and a page of plain text should not grow an
    ``[Image]`` line just because it was asked. A chunk that held nothing
    but such a marker is dropped rather than left empty, because an empty
    chunk is a permanent processing error.

    The token count is recounted for the chunks that changed, because the
    count bounds how much of a document one read may return, and a stale
    count would let a read run past its budget.
    """
    hints = {image.ref: image.describe_hint for image in images}
    merged: list[ExtractedChunk] = []
    for chunk in chunks:
        refs = picture_refs(chunk.text)
        if not refs:
            merged.append(chunk)
            continue
        text = chunk.text
        for ref in refs:
            hint = hints.get(ref, "picture")
            description = descriptions.get(ref, "").strip()
            if hint == "scan_page" and is_no_figure_reply(description):
                replacement = ""
            elif description:
                replacement = f"{DESCRIPTION_PREFIX}{description}{DESCRIPTION_SUFFIX}"
            else:
                replacement = UNDESCRIBED_PICTURE
            text = text.replace(picture_marker(ref), replacement, 1)
        if not text.strip():
            continue
        described_refs = [ref for ref in refs if descriptions.get(ref, "").strip()]
        if described_refs:
            provenance = dict(chunk.provenance)
            provenance["images"] = [
                {
                    "ref": ref,
                    "described": bool(descriptions.get(ref, "").strip()),
                }
                for ref in refs
            ]
            if model:
                provenance["image_model"] = model
            merged.append(
                replace(
                    chunk,
                    text=text,
                    token_count=count_tokens(text, tokenizer_name),
                    provenance=provenance,
                )
            )
        else:
            merged.append(replace(chunk, text=text, token_count=chunk.token_count))
    return merged
