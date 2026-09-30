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
    page_numbers: list[int]
    provenance: dict[str, Any]


@dataclass(frozen=True)
class ExtractionResult:
    document_json: bytes
    chunks: list[ExtractedChunk]
    extractor_version: str


class DocumentExtractor(Protocol):
    def extract(self, path: Path, *, media_type: str) -> ExtractionResult: ...
