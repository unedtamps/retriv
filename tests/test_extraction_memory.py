"""Telling "this page was hard to read" apart from "the sandbox ran out of room".

The sandbox turns a memory ceiling into a non-zero exit or a SIGKILL, and the
wording of an allocator failure travels out in its stderr. The worker's
coverage guardrail reads the difference: without it a memory ceiling is
reported as a lossy parse, and a retry burns another run on a deterministic
failure.
"""

from pathlib import Path
from typing import Any

import pytest

from app.documents.validation import PDF_MEDIA_TYPE
from app.extraction import sandbox as sandbox_module
from app.extraction.base import (
    ExtractionError,
    ExtractionResourceError,
    looks_like_allocation_failure,
)
from app.extraction.sandbox import SandboxedExtractor


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
        "OpenBLAS error: Memory allocation still failed after 10 retries, giving up.",
        "onnxruntime: out of memory: Failed to allocate memory for the model",
    ],
)
def test_allocator_wordings_are_recognised(message: str) -> None:
    assert looks_like_allocation_failure(message)


@pytest.mark.parametrize(
    "message",
    [
        "Page 4 is blank",
        "no such table: revenue",
        "OCR produced too few characters",
    ],
)
def test_ordinary_errors_are_not_mistaken_for_memory(message: str) -> None:
    assert not looks_like_allocation_failure(message)


def test_a_resource_failure_is_still_an_extraction_failure() -> None:
    """Callers that only know ``ExtractionError`` must keep working."""
    assert issubclass(ExtractionResourceError, ExtractionError)


def _sandboxed_extractor() -> SandboxedExtractor:
    return SandboxedExtractor(
        chunk_max_tokens=700,
        tokenizer_name="cl100k_base",
        scan_text_threshold=32,
        scan_page_describe=False,
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
        (-9, b""),
        (
            1,
            b"DefaultCPUAllocator: can't allocate memory: you tried to allocate 26214400 bytes",
        ),
    ],
)
def test_the_sandbox_reports_running_out_of_room_as_a_resource_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, return_code: int, stderr_text: bytes
) -> None:
    class FakeProcess:
        pid = 4321

        def __init__(self, *args: Any, **kwargs: Any) -> None:
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only memory failures get the limit advice; a broken document does not."""

    class FakeProcess:
        pid = 4321

        def __init__(self, *args: Any, **kwargs: Any) -> None:
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
