import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.documents.validation import DOCX_MEDIA_TYPE, PDF_MEDIA_TYPE
from app.extraction.docling import DoclingExtractor
from app.extraction.sandbox import SandboxedDoclingExtractor
from app.sections import encode_section_key

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
    # The stored key is what section completion queries, so it must be derived from
    # the path rather than left for the caller to reconstruct.
    assert result.chunks[0].section_key == encode_section_key(["Annual Report"])
    # This chunk carries body text, so the parser's own labels rule out heading-only.
    assert result.chunks[0].is_heading_only is False


@pytest.mark.integration
def test_a_heading_with_no_body_is_marked_from_parser_labels(
    tmp_path: Path, docling_extractor: DoclingExtractor
) -> None:
    """The structural signal replaces the old "is it under twelve tokens" guess.

    A heading emitted on its own is recorded as heading-only whatever its depth, so a
    deeply nested heading whose breadcrumb alone already exceeds any length threshold
    is no longer misread as body text. Measured against the tokenizer the extractor
    uses, this one is well past the old ceiling.
    """
    path = tmp_path / "nested.docx"
    document = Document()
    document.add_heading("Peraturan Menteri Keuangan Republik Indonesia", level=1)
    document.add_paragraph("Ketentuan umum.")
    document.add_heading("Pasal 16", level=2)
    document.add_paragraph("Kerangka umum.")
    document.add_heading("Kerangka Umum", level=3)
    document.save(path)

    result = docling_extractor.extract(path, media_type=DOCX_MEDIA_TYPE)

    heading_only = [chunk for chunk in result.chunks if chunk.is_heading_only]
    assert heading_only, "the childless heading should be emitted on its own"
    assert heading_only[0].section_path == [
        "Peraturan Menteri Keuangan Republik Indonesia",
        "Pasal 16",
        "Kerangka Umum",
    ]
    assert heading_only[0].section_key == encode_section_key(
        ["Peraturan Menteri Keuangan Republik Indonesia", "Pasal 16", "Kerangka Umum"]
    )
    assert any(
        chunk.section_path == ["Peraturan Menteri Keuangan Republik Indonesia", "Pasal 16"]
        and not chunk.is_heading_only
        for chunk in result.chunks
    ), "the parent section keeps its own prose in a chunk that is not heading-only"


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
    assert result.source_text_chars > 0


@pytest.mark.integration
def test_ocr_stays_enabled_for_every_pdf(
    tmp_path: Path, docling_extractor: DoclingExtractor
) -> None:
    """The OCR stage must not be gated off per file.

    Docling's PDF_AWARE_LAYOUT_REGIONS mode already skips regions backed by the file's
    own text layer, so a born-digital PDF never reaches Tesseract. Gating the stage off
    by text density was measured to change nothing for those files, while a document
    mixing born-digital and scanned pages averages out high enough to lose the scanned
    pages entirely -- silently, with no chunk text and no error.
    """
    text_pdf = tmp_path / "born-digital.pdf"
    text_pdf.write_bytes(
        text_layer_pdf(
            [
                "Regulation 4.01 The Administrator shall ensure that project",
                "documents are retained for a period of not less than three",
                "years after the financial year in which they were created.",
            ]
        )
    )

    result = docling_extractor.extract(text_pdf, media_type=PDF_MEDIA_TYPE)

    assert result.ocr_enabled is True
    assert result.source_text_chars > 0
    assert result.chunks

    docx: Any = Document()
    docx.add_heading("Annual Report", level=1)
    docx.add_paragraph("Operating costs remained stable.")
    docx_path = tmp_path / "report.docx"
    docx.save(docx_path)

    docx_result = docling_extractor.extract(docx_path, media_type=DOCX_MEDIA_TYPE)

    assert docx_result.ocr_enabled is False, "DOCX is never an OCR candidate"
    assert docx_result.source_text_chars == 0


@pytest.mark.integration
def test_ocr_runs_for_an_image_only_pdf(
    tmp_path: Path, docling_extractor: DoclingExtractor
) -> None:
    """A scan has no text layer of its own, so it still gets OCR."""
    image = Image.new("RGB", (1600, 1000), "white")
    ImageDraw.Draw(image).multiline_text(
        (80, 100),
        "Scanned financial regulation\nThe Treasurer shall be duly appointed",
        fill="black",
        font=ImageFont.load_default(size=48),
        spacing=28,
    )
    path = tmp_path / "scan.pdf"
    image.save(path, "PDF", resolution=150)

    result = docling_extractor.extract(path, media_type=PDF_MEDIA_TYPE)

    assert result.source_text_chars == 0
    assert result.ocr_enabled is True


@pytest.mark.integration
def test_a_heading_whose_section_is_empty_stays_searchable(
    tmp_path: Path, docling_extractor: DoclingExtractor
) -> None:
    """A heading with no body text must still reach the index.

    Without ``always_emit_headings`` the chunker consumes every heading as
    context-only and drops it, so a heading whose section has no body item leaves
    nothing searchable behind.
    """
    document: Any = Document()
    document.add_heading("Regulation 9.09", level=1)
    document.add_page_break()
    document.add_paragraph("This section was left deliberately empty.")
    path = tmp_path / "sparse-heading.docx"
    document.save(path)

    result = docling_extractor.extract(path, media_type=DOCX_MEDIA_TYPE)

    joined = " ".join(chunk.text for chunk in result.chunks)
    assert "Regulation 9.09" in joined


def text_layer_pdf(lines: list[str]) -> bytes:
    """A single born-digital page carrying ``lines`` of text.

    ``simple_pdf`` puts everything in one Tj operator, so long strings run off the
    page and come back truncated. This walks down the page instead, which is what a
    real text-layer PDF looks like to the density check.
    """
    operators = ["BT /F1 12 Tf"]
    for index, line in enumerate(lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        operators.append(f"1 0 0 1 72 {720 - index * 16} Tm ({escaped}) Tj")
    operators.append("ET")
    stream = "\n".join(operators).encode("ascii")
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


def simple_pdf(text: str) -> bytes:
    return text_layer_pdf([text])
