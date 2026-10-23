from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class ExtractionError(Exception):
    """Document extraction failed."""


@dataclass(frozen=True)
class ExtractedImage:
    """One picture lifted out of a document.

    The bytes are the picture as the parser cropped it, already rendered at
    the scale the vision model will read them at. ``ref`` is the parser's own
    handle for the picture, and it is what a chunk's text points at, so the
    description can be put back where the picture was.
    """

    ref: str
    page_number: int
    media_type: str
    data: bytes
    caption: str = ""
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class ExtractedChunk:
    text: str
    token_count: int
    section_path: list[str]
    section_key: str
    is_heading_only: bool
    page_numbers: list[int]
    provenance: dict[str, Any]


@dataclass(frozen=True)
class ExtractionResult:
    document_json: bytes
    chunks: list[ExtractedChunk]
    extractor_version: str
    # Whether the OCR stage actually ran, and how much text the source file's own
    # text layer carried. The pair lets the caller tell "this document had no text"
    # apart from "the parser lost the text", which are opposite failures.
    ocr_enabled: bool = False
    source_text_chars: int = 0
    # The pictures the parser found, in reading order. Empty when the stage is
    # off or the document has no pictures.
    images: list[ExtractedImage] = field(default_factory=list)


class DocumentExtractor(Protocol):
    def extract(self, path: Path, *, media_type: str) -> ExtractionResult: ...
