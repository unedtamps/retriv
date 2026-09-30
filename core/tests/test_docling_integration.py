import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.contracts.events import DOCX_MEDIA_TYPE, PDF_MEDIA_TYPE
from app.extraction.docling import DoclingExtractor
from app.extraction.sandbox import SandboxedDoclingExtractor

pytest.importorskip("docling")
docx_module = pytest.importorskip("docx")
image_module = pytest.importorskip("PIL.Image")
image_draw_module = pytest.importorskip("PIL.ImageDraw")
image_font_module = pytest.importorskip("PIL.ImageFont")
Document: Any = docx_module.Document
Image: Any = image_module
ImageDraw: Any = image_draw_module
ImageFont: Any = image_font_module


def available_ocr_language() -> str:
    if shutil.which("tesseract") is None:
        pytest.skip("Tesseract CLI is not installed")
    completed = subprocess.run(
        ["tesseract", "--list-langs"],
        check=True,
        capture_output=True,
        text=True,
    )
    installed = set(completed.stdout.splitlines()[1:])
    for language in ("ind", "eng", "afr"):
        if language in installed:
            return language
    pytest.skip("No suitable Tesseract language is installed")


@pytest.fixture(scope="module")
def docling_extractor() -> DoclingExtractor:
    return DoclingExtractor(
        ocr_languages=[available_ocr_language()],
        chunk_max_tokens=700,
        tokenizer_name="cl100k_base",
    )


@pytest.mark.integration
def test_extracts_native_pdf_with_page_provenance(
    tmp_path: Path, docling_extractor: DoclingExtractor
) -> None:
    path = tmp_path / "report.pdf"
    path.write_bytes(simple_pdf("Laporan Tahunan Revenue increased twenty percent"))

    result = docling_extractor.extract(path, media_type=PDF_MEDIA_TYPE)

    artifact = json.loads(result.document_json)
    assert artifact["schema_name"] == "DoclingDocument"
    assert len(result.chunks) == 1
    assert "Revenue increased" in result.chunks[0].text
    assert result.chunks[0].page_numbers == [1]


@pytest.mark.integration
def test_extracts_scanned_pdf_with_ocr(
    tmp_path: Path, docling_extractor: DoclingExtractor
) -> None:
    image = Image.new("RGB", (1600, 1000), "white")
    ImageDraw.Draw(image).multiline_text(
        (80, 100),
        (
            "Scanned OCR context for annual reporting\n"
            "Revenue increased during the current year\n"
            "Operating costs remained stable this quarter\n"
            "This page contains only a scanned bitmap image"
        ),
        fill="black",
        font=ImageFont.load_default(size=48),
        spacing=28,
    )
    path = tmp_path / "scanned.pdf"
    image.save(path, "PDF", resolution=150)

    result = docling_extractor.extract(path, media_type=PDF_MEDIA_TYPE)

    assert result.chunks
    assert "context" in " ".join(chunk.text for chunk in result.chunks).lower()
    assert result.chunks[0].page_numbers == [1]


@pytest.mark.integration
def test_extracts_docx_hierarchy(
    tmp_path: Path, docling_extractor: DoclingExtractor
) -> None:
    path = tmp_path / "report.docx"
    document = Document()
    document.add_heading("Annual Report", level=1)
    document.add_paragraph("Operating costs remained stable.")
    document.save(path)

    result = docling_extractor.extract(path, media_type=DOCX_MEDIA_TYPE)

    assert len(result.chunks) == 1
    assert "Operating costs remained stable" in result.chunks[0].text
    assert result.chunks[0].section_path == ["Annual Report"]


@pytest.mark.integration
def test_sandboxed_extraction_returns_bounded_result(tmp_path: Path) -> None:
    path = tmp_path / "sandboxed.pdf"
    path.write_bytes(simple_pdf("Sandboxed annual report context"))
    extractor = SandboxedDoclingExtractor(
        ocr_languages=[available_ocr_language()],
        chunk_max_tokens=700,
        tokenizer_name="cl100k_base",
        artifacts_path=None,
        timeout_seconds=180,
        memory_limit_bytes=8 * 1024 * 1024 * 1024,
        max_processes=4096,
        max_artifact_bytes=10 * 1024 * 1024,
        max_chunk_payload_bytes=10 * 1024 * 1024,
        max_extracted_text_bytes=5 * 1024 * 1024,
        max_chunks=100,
    )

    result = extractor.extract(path, media_type=PDF_MEDIA_TYPE)

    assert "Sandboxed annual report context" in result.chunks[0].text
    assert result.chunks[0].page_numbers == [1]


def simple_pdf(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, content in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode())
        output.extend(content)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode()
    )
    return bytes(output)
