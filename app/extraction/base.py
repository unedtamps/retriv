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


# How the allocator and the tensor libraries word the same failure. Docling
# reports them as ordinary per-page conversion errors and still exits
# successfully, so the only way to tell "this page was hard to read" from "the
# process ran out of room" is the text of the message.
ALLOCATION_FAILURE_MARKERS = (
    "can't allocate memory",
    "cannot allocate memory",
    "cannot allocate",
    # OpenBLAS gives up with this wording when the address-space limit is below
    # what importing the numeric stack needs.
    "memory allocation",
    # dlopen of a shared library that could not be mapped: also the limit, not a
    # missing file.
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


@dataclass(frozen=True)
class ExtractedImage:
    """One picture lifted out of a document.

    The bytes are the picture as the extractor cropped it, already rendered
    at the scale the vision model will read them at. A DOCX picture arrives
    cropped inside the parsed document; a PDF picture is cropped from the
    source file afterwards, so the parser never holds its bytes. ``ref`` is
    the parser's own handle for the picture, and it is what a chunk's text
    points at, so the description can be put back where the picture was.
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
    # How many pages the parser saw, not how many it managed to read. The
    # coverage guardrail reports both, because "1 of 182 pages" and "1 of 2
    # pages" describe the same ratio and completely different incidents.
    page_count: int = 0
    # The pictures the parser found, in reading order. Empty when the stage is
    # off or the document has no pictures.
    images: list[ExtractedImage] = field(default_factory=list)


class DocumentExtractor(Protocol):
    def extract(self, path: Path, *, media_type: str) -> ExtractionResult: ...
