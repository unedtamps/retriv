import io
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.documents.validation import DOCX_MEDIA_TYPE, PDF_MEDIA_TYPE
from app.extraction.docling import DoclingExtractor
from app.extraction.images import picture_refs
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
def test_a_long_table_is_split_on_rows_and_repeats_its_header(
    tmp_path: Path, docling_extractor: DoclingExtractor
) -> None:
    """A table is cut on row boundaries, and every part says which columns it holds.

    The chunker used to receive a table as one line of "row, column = value" text, so a
    table longer than the budget was cut wherever the tokens fell -- inside a cell, in
    the middle of a number. Measured on this corpus, that was half the table chunks in
    the 10-K, ending on fragments like "Page No. =".

    A table is written one grid row per line instead, and the chunker cuts on those
    lines. The header is repeated in every part, because a part that does not say which
    columns it holds is a table of anonymous numbers.
    """
    path = tmp_path / "table.docx"
    document = Document()
    document.add_heading("Appendix A", level=1)
    document.add_paragraph("Paragraf sebelum tabel.")
    table = document.add_table(rows=61, cols=3)
    table.cell(0, 0).text = "Posisi"
    table.cell(0, 1).text = "2025"
    table.cell(0, 2).text = "2024"
    for index in range(1, 61):
        table.cell(index, 0).text = f"Baris {index}"
        table.cell(index, 1).text = f"nilai {index * 11}"
        table.cell(index, 2).text = f"nilai {index * 12}"
    document.add_paragraph("Paragraf selepas tabel.")
    document.save(path)

    result = docling_extractor.extract(path, media_type=DOCX_MEDIA_TYPE)

    table_chunks = [chunk for chunk in result.chunks if "Posisi | 2025 | 2024" in chunk.text]
    assert len(table_chunks) > 1, "a table this long does not fit in one chunk"

    seen_rows: list[int] = []
    for chunk in table_chunks:
        lines = chunk.text.split("\n")
        assert "Posisi | 2025 | 2024" in lines, "every part repeats the header"
        rows = [line for line in lines if " = " in line]
        assert rows, "a part of a table holds at least one row"
        for row in rows:
            # Every row is whole: a cut cell would leave a half-written number.
            match = re.fullmatch(
                r"Baris (\d+), 2025 = nilai (\d+)\. Baris \1, 2024 = nilai (\d+)", row
            )
            assert match is not None, f"row was cut in half: {row!r}"
            seen_rows.append(int(match.group(1)))

    assert seen_rows == sorted(seen_rows), "the rows stay in reading order"
    assert seen_rows == list(range(1, 61)), "every row is present exactly once"

    # The table keeps its place: the prose before it comes first and the prose
    # after it comes last, with the table's parts in between.
    order = [
        "before" if "Paragraf sebelum tabel." in chunk.text else ""
        for chunk in result.chunks
    ]
    assert "before" in order
    before = order.index("before")
    assert all(
        "Posisi | 2025 | 2024" not in chunk.text
        for chunk in result.chunks[:before]
    ), "the table does not precede the prose that introduces it"


@pytest.mark.integration
def test_a_table_that_fits_stays_in_one_chunk(
    tmp_path: Path, docling_extractor: DoclingExtractor
) -> None:
    """A small table is not split, and its rows are one line each.

    The row-wise format is not only for tables that have to be cut. A table that fits
    keeps its rows apart, so a reader can see where one row ends and the next begins.
    """
    path = tmp_path / "small_table.docx"
    document = Document()
    document.add_heading("Ringkasan", level=1)
    table = document.add_table(rows=3, cols=2)
    table.cell(0, 0).text = "Posisi"
    table.cell(0, 1).text = "Jumlah"
    table.cell(1, 0).text = "Pendapatan"
    table.cell(1, 1).text = "1.200"
    table.cell(2, 0).text = "Beban"
    table.cell(2, 1).text = "800"
    document.save(path)

    result = docling_extractor.extract(path, media_type=DOCX_MEDIA_TYPE)

    assert len(result.chunks) == 1
    lines = result.chunks[0].text.split("\n")
    assert "Posisi | Jumlah" in lines
    assert "Pendapatan, Jumlah = 1.200" in lines
    assert "Beban, Jumlah = 800" in lines


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


# --- pictures ------------------------------------------------------------


@pytest.fixture(scope="module")
def picture_extractor() -> DoclingExtractor:
    return DoclingExtractor(
        ocr_languages=[available_ocr_language()],
        chunk_max_tokens=700,
        tokenizer_name="cl100k_base",
        picture_images_enabled=True,
        image_render_scale=2.0,
        image_min_pixels=10_000,
        max_images=10,
    )


def pdf_with_a_figure() -> bytes:
    """A one-page PDF with a figure on it.

    Built with PyMuPDF rather than by hand, because a hand-built PDF with an
    embedded image is a page of cross-reference offsets, and the parser is
    not what this test is about.
    """
    import pymupdf

    figure = Image.new("RGB", (600, 300), (30, 90, 160))
    draw = ImageDraw.Draw(figure)
    draw.ellipse([150, 60, 450, 240], fill=(240, 200, 60))
    draw.text((210, 265), "REVENUE BY SEGMENT", fill=(255, 255, 255))

    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 80), "3.2 Financial Performance", fontsize=16, fontname="helv")
    page.insert_text(
        (72, 110),
        "The chart shows net revenue by segment for fiscal 2015.",
        fontsize=10,
        fontname="helv",
    )
    picture_bytes = io.BytesIO()
    figure.save(picture_bytes, format="PNG")
    page.insert_image(pymupdf.Rect(72, 130, 472, 280), stream=picture_bytes.getvalue())
    page.insert_text(
        (72, 300),
        "Figure 1: Net revenue by segment (in millions).",
        fontsize=9,
        fontname="helv",
    )
    page.insert_text(
        (72, 330),
        "Revenue grew across all three reported segments during the year.",
        fontsize=10,
        fontname="helv",
    )
    output = document.tobytes()
    document.close()
    return output


@pytest.mark.integration
def test_a_pdf_with_a_figure_yields_the_picture_and_marks_its_chunk(
    tmp_path: Path, picture_extractor: DoclingExtractor
) -> None:
    """The picture has to survive the parser for anything to be done with it.

    It comes back as bytes the model can look at, and the chunk that holds
    it says which picture it is holding, so the description can be put back
    on the line the picture was on.
    """
    path = tmp_path / "report.pdf"
    path.write_bytes(pdf_with_a_figure())

    result = picture_extractor.extract(path, media_type=PDF_MEDIA_TYPE)

    assert len(result.images) == 1
    image = result.images[0]
    assert image.media_type == "image/png"
    assert image.data.startswith(b"\x89PNG")
    assert image.page_number == 1
    assert image.width > 0 and image.height > 0

    marked = [chunk for chunk in result.chunks if picture_refs(chunk.text)]
    assert len(marked) == 1
    assert picture_refs(marked[0].text) == [image.ref]
    # the picture sits on the page the figure is on
    assert marked[0].page_numbers == [1]

    # The artifact keeps what the parser made of the file, without the
    # picture's bytes: the picture is described and then dropped, and a
    # base64 PNG in the middle of the artifact says nothing the description
    # does not.
    assert b"base64" not in result.document_json


@pytest.mark.integration
def test_a_document_indexed_without_the_picture_stage_carries_no_marker(
    tmp_path: Path,
) -> None:
    """With the stage off, nothing about pictures reaches the chunk text.

    A marker would be wrong here: nothing would ever come and replace it,
    and it would be embedded into the document as leftover syntax.
    """
    path = tmp_path / "report.pdf"
    path.write_bytes(pdf_with_a_figure())
    extractor = DoclingExtractor(
        ocr_languages=[available_ocr_language()],
        chunk_max_tokens=700,
        tokenizer_name="cl100k_base",
        picture_images_enabled=False,
    )

    result = extractor.extract(path, media_type=PDF_MEDIA_TYPE)

    assert result.images == []
    assert all("PICTURE" not in chunk.text for chunk in result.chunks)


@pytest.mark.integration
def test_a_figure_below_the_pixel_floor_is_not_collected(tmp_path: Path) -> None:
    """A picture that small is a logo, a rule or an ornament.

    It is not worth an API call, and describing it would add noise to the
    search.
    """
    path = tmp_path / "report.pdf"
    path.write_bytes(pdf_with_a_figure())
    extractor = DoclingExtractor(
        ocr_languages=[available_ocr_language()],
        chunk_max_tokens=700,
        tokenizer_name="cl100k_base",
        picture_images_enabled=True,
        image_render_scale=2.0,
        # A floor no picture of this size can clear.
        image_min_pixels=100_000_000,
        max_images=10,
    )

    result = extractor.extract(path, media_type=PDF_MEDIA_TYPE)

    assert result.images == []


@pytest.mark.integration
def test_the_sandbox_carries_the_picture_back_out(
    tmp_path: Path, picture_extractor: DoclingExtractor
) -> None:
    """The picture has to survive the trip through the sandbox.

    The parser runs in a subprocess, so the picture comes back as a file
    the parent reads, not as bytes inside the chunk payload.
    """
    path = tmp_path / "report.pdf"
    path.write_bytes(pdf_with_a_figure())
    extractor = SandboxedDoclingExtractor(
        ocr_languages=[available_ocr_language()],
        chunk_max_tokens=700,
        tokenizer_name="cl100k_base",
        artifacts_path=None,
        timeout_seconds=600,
        memory_limit_bytes=8 * 1024 * 1024 * 1024,
        max_processes=4096,
        max_artifact_bytes=10 * 1024 * 1024,
        max_chunk_payload_bytes=10 * 1024 * 1024,
        max_extracted_text_bytes=5 * 1024 * 1024,
        max_chunks=100,
        picture_images_enabled=True,
        image_render_scale=2.0,
        image_min_pixels=10_000,
        max_images=10,
        max_image_bytes=5 * 1024 * 1024,
        max_total_image_bytes=50 * 1024 * 1024,
    )

    result = extractor.extract(path, media_type=PDF_MEDIA_TYPE)

    assert len(result.images) == 1
    assert result.images[0].data.startswith(b"\x89PNG")
    assert picture_refs(result.chunks[0].text) == [result.images[0].ref]
