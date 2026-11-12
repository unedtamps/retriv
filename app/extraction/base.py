from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class ExtractionError(Exception):
    """Document extraction failed."""


class ExtractionResourceError(ExtractionError):
    """Extraction died because the parser ran out of memory.

    Kept apart from ``ExtractionError`` because the two mean opposite things
    to the caller. A malformed document fails the same way twice, and a retry
    only burns another parser run. Running out of room is a property of the
    sandbox's configured limit, so the operator can act on it -- but retrying
    under the same limit fails identically, which is why the worker treats it
    as a deterministic failure rather than a transient one.
    """


ALLOCATION_FAILURE_MARKERS = (
    "can't allocate memory",
    "cannot allocate memory",
    "cannot allocate",
    "memory allocation",
    "failed to map segment",
    "failed to allocate",
    "defaultcpuallocator",
    "std::bad_alloc",
    "memoryerror",
    "out of memory",
)


def looks_like_allocation_failure(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in ALLOCATION_FAILURE_MARKERS)


class TabularTable(Protocol):
    """What the row store needs from a CSV sheet, workbook sheet or JSON table."""

    @property
    def name(self) -> str: ...

    @property
    def columns(self) -> list[str]: ...

    @property
    def rows(self) -> Iterable[Mapping[str, Any]]: ...


@dataclass(frozen=True)
class ExtractedImage:
    """One picture lifted out of a document.

    The bytes are already rendered at the scale the vision model reads them
    at: a DOCX picture arrives cropped inside the parsed document, a PDF
    picture is cropped from the source file afterwards. ``ref`` is what a
    chunk's text points at, so a description can be put back in place.
    """

    ref: str
    page_number: int
    media_type: str
    data: bytes
    caption: str = ""
    width: int = 0
    height: int = 0
    bbox: tuple[float, float, float, float] | None = None
    """Normalized page box, when the crop came from a page with coordinates."""
    describe_hint: str = "picture"
    """Which prompt the vision stage uses: ``picture`` for a lifted figure,
    ``scan_page`` for a full scanned page that may hold figures."""


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
    ocr_enabled: bool = False
    source_text_chars: int = 0
    page_count: int = 0
    images: list[ExtractedImage] = field(default_factory=list)
    image_descriptions: dict[str, str] | None = None
    """Per-picture description the vision stage produced, ref-keyed.

    The chunk text carries the words inline, but it cannot say which ref
    they belonged to once the markers are replaced; the persistence stage
    needs the mapping to store each picture's description beside it.
    """


class DocumentExtractor(Protocol):
    def extract(self, path: Path, *, media_type: str) -> ExtractionResult: ...
