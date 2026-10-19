from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class ExtractionError(Exception):
    """Document extraction failed."""


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


class DocumentExtractor(Protocol):
    def extract(self, path: Path, *, media_type: str) -> ExtractionResult: ...
