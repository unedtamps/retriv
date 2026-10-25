"""Telling "this page was hard to read" apart from "the parser ran out of room".

Both arrive as Docling per-page conversion errors, and a partial conversion still
returns ``status=partial_success`` with a document attached, so the status alone
cannot distinguish them. The worker's coverage guardrail reads the difference:
without it a memory ceiling is reported as a lossy parse.
"""

from types import SimpleNamespace

import pytest

from app.documents.validation import PDF_MEDIA_TYPE
from app.extraction import sandbox as sandbox_module
from app.extraction.base import (
    ExtractionError,
    ExtractionResourceError,
    looks_like_allocation_failure,
)
from app.extraction.docling import _allocation_failure
from app.extraction.sandbox import SandboxedDoclingExtractor


def test_the_production_allocation_message_is_recognised() -> None:
    """The exact text from the 182-page 10-K that failed at an 8 GiB limit."""
    message = (
        "enforce fail at alloc_cpu.cpp:127] err == 0. DefaultCPUAllocator: "
        "can't allocate memory: you tried to allocate 26214400 bytes. "
        "Error code 12 (Cannot allocate memory)"
    )
    assert looks_like_allocation_failure(message)


@pytest.mark.parametrize(
    "message",
    [
        "MemoryError",
        "std::bad_alloc",
        "Cannot allocate memory",
        "CUDA out of memory",
        "torch.OutOfMemoryError: out of memory",
        "OpenBLAS error: Memory allocation still failed after 10 retries, giving up.",
    ],
)
def test_allocator_wordings_are_recognised(message: str) -> None:
    assert looks_like_allocation_failure(message)


@pytest.mark.parametrize(
    "message",
    [
        "Page 4 is blank",
        "Table structure model returned no cells",
        "OCR produced too few characters",
    ],
)
def test_ordinary_conversion_errors_are_not_mistaken_for_memory(
    message: str,
) -> None:
    assert not looks_like_allocation_failure(message)


def test_the_first_offending_page_is_reported_with_its_number() -> None:
    errors = [
        SimpleNamespace(page_no=1, error_message="Page 1 is blank"),
        SimpleNamespace(
            page_no=3,
            error_message=(
                "DefaultCPUAllocator: can't allocate memory: you tried to "
                "allocate 52428800 bytes"
            ),
        ),
        SimpleNamespace(page_no=4, error_message="no such page"),
    ]
    failure = _allocation_failure(errors)
    assert failure is not None
    assert failure.startswith("page 3:")
    assert "52428800" in failure


def test_a_clean_error_list_reports_nothing() -> None:
    assert _allocation_failure([]) is None
    assert _allocation_failure([SimpleNamespace(page_no=2, error_message="blank")]) is None


def test_a_partial_conversion_that_hit_the_limit_is_not_returned_as_a_result(
    tmp_path, monkeypatch
) -> None:
    """Docling still hands back a document, so the caller has to refuse it.

    This is the shape of the production failure: status=partial_success, one
    error per unread page, a document holding only page 2, and a 182-page file.
    """
    from app.extraction.docling import DoclingExtractor

    extractor = DoclingExtractor.__new__(DoclingExtractor)
    extractor._version = "test-docling"
    extractor.measure_pdf_text_layer = lambda path: (608_897, 182)  # type: ignore[method-assign]
    extractor._converter = SimpleNamespace(
        convert=lambda path: SimpleNamespace(
            status=SimpleNamespace(value="partial_success"),
            errors=[
                SimpleNamespace(
                    page_no=3,
                    error_message=(
                        "DefaultCPUAllocator: can't allocate memory: you tried to "
                        "allocate 26214400 bytes. Error code 12 (Cannot allocate memory)"
                    ),
                )
            ],
            document=SimpleNamespace(pages={page: None for page in range(1, 183)}),
        )
    )
    path = tmp_path / "10-K.pdf"
    path.write_bytes(b"%PDF-1.7\n")

    with pytest.raises(ExtractionResourceError) as caught:
        extractor.extract(path, media_type=PDF_MEDIA_TYPE)

    message = str(caught.value)
    assert "182 pages" in message
    assert "page 3" in message
    assert "26214400" in message


def test_a_resource_failure_is_still_an_extraction_failure() -> None:
    """Callers that only know ``ExtractionError`` must keep working."""
    assert issubclass(ExtractionResourceError, ExtractionError)


def _sandboxed_extractor() -> SandboxedDoclingExtractor:
    return SandboxedDoclingExtractor(
        ocr_languages=["eng"],
        chunk_max_tokens=700,
        tokenizer_name="cl100k_base",
        artifacts_path=None,
        timeout_seconds=60,
        memory_limit_bytes=8 * 1024**3,
        max_processes=4096,
        max_artifact_bytes=1_000_000,
        max_chunk_payload_bytes=1_000_000,
        max_extracted_text_bytes=1_000_000,
        max_chunks=100,
    )


@pytest.mark.parametrize(
    ("return_code", "stderr_text"),
    [
        # The cgroup's OOM killer.
        (-9, b""),
        # The address-space limit: docling reports it per page and exits 1.
        (
            1,
            b"DefaultCPUAllocator: can't allocate memory: you tried to "
            b"allocate 26214400 bytes",
        ),
    ],
)
def test_the_sandbox_reports_running_out_of_room_as_a_resource_failure(
    tmp_path, monkeypatch, return_code: int, stderr_text: bytes
) -> None:
    class FakeProcess:
        pid = 4321

        def __init__(self, *args, **kwargs) -> None:
            if stderr_text:
                kwargs["stderr"].write(stderr_text)
                kwargs["stderr"].flush()

        def wait(self, timeout: float | None = None) -> int:
            return return_code

    monkeypatch.setattr(sandbox_module.subprocess, "Popen", FakeProcess)
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.7\n")

    with pytest.raises(ExtractionResourceError) as caught:
        _sandboxed_extractor().extract(path, media_type=PDF_MEDIA_TYPE)

    message = str(caught.value)
    assert "8.0 GiB" in message, "the message must name the limit to raise"


def test_an_ordinary_sandbox_failure_stays_an_extraction_failure(
    tmp_path, monkeypatch
) -> None:
    """Only memory failures get the limit advice; a broken document does not."""

    class FakeProcess:
        pid = 4321

        def __init__(self, *args, **kwargs) -> None:
            kwargs["stderr"].write(b"pymupdf: cannot open document")
            kwargs["stderr"].flush()

        def wait(self, timeout: float | None = None) -> int:
            return 1

    monkeypatch.setattr(sandbox_module.subprocess, "Popen", FakeProcess)
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.7\n")

    with pytest.raises(ExtractionError) as caught:
        _sandboxed_extractor().extract(path, media_type=PDF_MEDIA_TYPE)

    assert not isinstance(caught.value, ExtractionResourceError)
    assert "GiB" not in str(caught.value)
