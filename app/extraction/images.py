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
from dataclasses import replace

from app.extraction.base import ExtractedChunk, ExtractedImage

PICTURE_MARKER_PATTERN = re.compile(r"\[\[PICTURE:([^\[\]]+)\]\]")
"""Matches the marker the parser leaves behind for a picture."""

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
