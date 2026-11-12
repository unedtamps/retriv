"""The native markdown extractor and the chunker behind it.

The unit tests pin the packer's contract -- reading order, the section
path, the page numbers, the token budget, the row-wise table split -- and
the integration tests run real PDFs and a real DOCX through the extractor,
including a scanned page read by OCR and the sandboxed subprocess.
"""

import re
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from app.documents.validation import DOCX_MEDIA_TYPE, PDF_MEDIA_TYPE
from app.extraction import markdown_ir
from app.extraction.base import ExtractionResult
from app.extraction.chunker import MarkdownChunker
from app.extraction.docx_ir import build_docx_blocks
from app.extraction.images import picture_marker, picture_refs
from app.extraction.markdown_ir import (
    DocumentBlock,
    PageLine,
    SourceRegion,
    parse_markdown_blocks,
    promote_landmarks,
)
from app.extraction.native import EXTRACTOR_VERSION, NativeMarkdownExtractor
from app.extraction.tables import markdown_table_cells, row_wise_table_text
from tests.document_samples import (
    borderless_table_cells,
    borderless_table_pdf_bytes,
    image_pdf_bytes,
    landmark_pdf_bytes,
    pptx_bytes,
    scanned_pdf_bytes,
    table_pdf_bytes,
    two_column_pdf_bytes,
    vector_bar_chart_pdf_bytes,
    vector_chart_pdf_bytes,
    vector_table_pdf_bytes,
    vector_zebra_table_pdf_bytes,
)


def write_sample(tmp_path: Path, data: bytes, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def an_extractor(**overrides: Any) -> NativeMarkdownExtractor:
    options: dict[str, Any] = {
        "chunk_max_tokens": 700,
        "tokenizer_name": "cl100k_base",
        "picture_images_enabled": False,
    }
    options.update(overrides)
    return NativeMarkdownExtractor(**options)


def block(kind: str, text: str, page_no: int = 1, level: int = 0) -> DocumentBlock:
    return DocumentBlock(kind=kind, text=text, page_no=page_no, level=level)


def a_chunker(**overrides: Any) -> MarkdownChunker:
    options: dict[str, Any] = {"chunk_max_tokens": 700, "tokenizer_name": "cl100k_base"}
    options.update(overrides)
    return MarkdownChunker(**options)


def test_a_landmark_line_becomes_a_heading_at_its_own_level() -> None:
    promoted = promote_landmarks(
        [
            "PART II",
            "Item 7A. Quantitative Disclosures",
            "Pasal 16",
            "Regulation 2.02",
            "Chapter C: Accountability",
            "The commission may extend the deadline.",
        ]
    )

    assert promoted[0] == "# PART II"
    assert promoted[1] == "## Item 7A. Quantitative Disclosures"
    assert promoted[2] == "## Pasal 16"
    assert promoted[3] == "### Regulation 2.02"
    assert promoted[4] == "# Chapter C: Accountability"
    assert promoted[5] == "The commission may extend the deadline."


def test_a_long_line_is_never_promoted() -> None:
    body = "Item " + "one thousand words of ordinary prose " * 6
    assert promote_landmarks([body]) == [body]


def test_markdown_blocks_are_parsed_in_reading_order() -> None:
    blocks = parse_markdown_blocks(
        "## PART I\n\nSome prose follows.\n\n|Year|Revenue|\n|---|---|\n|2024|1000|\n",
        page_no=3,
    )

    assert [(b.kind, b.text) for b in blocks] == [
        ("heading", "PART I"),
        ("text", "Some prose follows."),
        ("table", "|Year|Revenue|\n|---|---|\n|2024|1000|"),
    ]
    assert all(b.page_no == 3 for b in blocks)


def test_a_dead_column_is_dropped_from_a_markdown_table() -> None:
    cells = markdown_table_cells(
        "|Name||Age|\n|---|---|---|\n|Ada||36|\n|Alan||41|\n"
    )

    assert cells == (["Name", "Age"], [["Ada", "36"], ["Alan", "41"]])


def test_the_row_wise_table_repeats_the_row_label_per_cell() -> None:
    text = row_wise_table_text(
        ["Year", "Revenue", "Laba"],
        [["2024", "1000", "300"]],
        has_header=True,
    )

    assert text.splitlines() == [
        "Year | Revenue | Laba",
        "2024, Revenue = 1000. 2024, Laba = 300",
    ]


def test_a_heading_before_content_prepends_the_section_to_the_chunk() -> None:
    chunks = a_chunker().chunk(
        [
            block("heading", "PART I", level=1),
            block("heading", "Item 1. Business", level=2),
            block("text", "The company sells software."),
        ]
    )

    assert len(chunks) == 2
    assert chunks[0].is_heading_only is True
    assert chunks[0].text == "PART I"
    assert chunks[1].section_path == ["PART I", "Item 1. Business"]
    assert chunks[1].text.startswith("PART I > Item 1. Business\n\n")
    assert chunks[1].section_key == '["PART I","Item 1. Business"]'


def test_a_deep_heading_replaces_its_siblings_but_keeps_its_ancestors() -> None:
    chunks = a_chunker().chunk(
        [
            block("heading", "PART I", level=1),
            block("heading", "Item 1A. Risk Factors", level=2),
            block("text", "Competition is a risk."),
            block("heading", "Pasal 16", level=2),
            block("text", "Arsip wajib disimpan."),
        ]
    )

    assert [c.section_path for c in chunks if not c.is_heading_only] == [
        ["PART I", "Item 1A. Risk Factors"],
        ["PART I", "Pasal 16"],
    ]


def test_the_budget_splits_a_table_at_row_boundaries_repeating_the_header() -> None:
    chunker = a_chunker(chunk_max_tokens=25)
    rows = [
        block("text", f"Row number {index} with a little more prose.", level=0)
        for index in range(6)
    ]
    chunks = chunker.chunk(
        [
            block("heading", "Appendix", level=1),
            block("table", "|Item|Count|\n|---|---|\n|A|1|\n|B|2|\n|C|3|"),
            *rows,
        ]
    )

    assert len(chunks) > 2
    table_parts = [c for c in chunks if "Item | Count" in c.text]
    assert len(table_parts) >= 2, "the header must be repeated in every table part"
    assert chunks[0].section_path == ["Appendix"]
    for part in chunks:
        assert part.token_count <= 40


def test_page_numbers_come_from_the_blocks_a_chunk_holds() -> None:
    chunks = a_chunker().chunk(
        [
            block("heading", "PART I", page_no=1, level=1),
            block("text", "First page prose.", page_no=1),
            block("text", "Second page prose.", page_no=2),
        ]
    )

    assert chunks[-1].page_numbers == [1, 2]
    assert chunks[-1].provenance["page_regions"] == [{"page_no": 1}, {"page_no": 2}]


def test_a_docx_chunk_has_no_pages_and_an_empty_section_key_at_the_top() -> None:
    chunks = a_chunker().chunk([block("text", "No pages here.", page_no=0)])

    assert chunks[0].page_numbers == []
    assert chunks[0].provenance["page_regions"] == []


def test_chunk_provenance_carries_normalized_source_regions() -> None:
    region = SourceRegion(
        page_no=2,
        bbox=(0.1, 0.2, 0.5, 0.3),
    )
    chunks = a_chunker().chunk(
        [
            DocumentBlock(
                kind="text",
                text="A cited paragraph.",
                page_no=2,
                regions=(region,),
            )
        ]
    )

    assert chunks[0].provenance["regions"] == [
        {
            "page_no": 2,
            "bbox": [0.1, 0.2, 0.5, 0.3],
            "kind": "text",
            "ref": "",
            "anchor": "",
        }
    ]


def test_page_lines_attach_to_the_block_that_contains_their_text() -> None:
    blocks = parse_markdown_blocks(
        "A cited paragraph.\n",
        page_no=2,
        lines=(
            PageLine(
                text="A cited paragraph.",
                page_no=2,
                bbox=(0.1, 0.2, 0.5, 0.3),
            ),
        ),
    )

    assert blocks[0].regions[0].page_no == 2
    assert blocks[0].regions[0].bbox == (0.1, 0.2, 0.5, 0.3)


def test_ocr_lines_keep_normalized_boxes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pymupdf

    class FakeOcr:
        def read(self, pixmap: pymupdf.Pixmap) -> list[tuple[float, float, float, float, str]]:
            del pixmap
            return [
                (10.0, 20.0, 100.0, 30.0, "Hello"),
                (120.0, 20.0, 80.0, 30.0, "world"),
                (10.0, 80.0, 100.0, 30.0, "Second"),
            ]

    monkeypatch.setattr(markdown_ir, "_ocr_engine", lambda: FakeOcr())
    with pymupdf.open(stream=scanned_pdf_bytes(), filetype="pdf") as pdf:
        lines = markdown_ir.ocr_page_lines(pdf, 0)

    assert [line.text for line in lines] == ["Hello world", "Second"]
    for line in lines:
        assert line.bbox is not None
        x0, y0, x1, y1 = line.bbox
        assert 0 <= x0 < x1 <= 1
        assert 0 <= y0 < y1 <= 1


def test_an_image_block_becomes_a_marker_line() -> None:
    chunks = a_chunker().chunk(
        [block("text", "Prose."), block("image", picture_marker("image-p1-0"))]
    )

    assert picture_refs(chunks[0].text) == ["image-p1-0"]


def test_a_single_line_longer_than_the_budget_is_cut_at_tokens() -> None:
    chunks = a_chunker(chunk_max_tokens=24).chunk(
        [block("text", "word " * 120)]
    )

    assert len(chunks) >= 2
    assert all(c.token_count <= 24 for c in chunks)


def test_extracted_pdf_chunks_keep_reading_order_sections_and_pages(
    tmp_path: Path,
) -> None:
    path = write_sample(tmp_path, landmark_pdf_bytes(), "landmark.pdf")
    result = an_extractor().extract(path, media_type=PDF_MEDIA_TYPE)

    assert result.extractor_version == EXTRACTOR_VERSION
    assert result.ocr_enabled is True
    assert result.page_count == 2
    assert result.source_text_chars > 0
    assert result.chunks
    body = [c for c in result.chunks if not c.is_heading_only]
    assert body[0].section_path == ["PART I", "Item 1. Business"]
    assert "Microsoft develops and licenses software." in body[0].text
    assert body[0].page_numbers == [1]
    indonesian = [c for c in body if "arsip" in c.text]
    assert indonesian and indonesian[0].section_path == ["PART I", "Pasal 16"]
    assert indonesian[0].page_numbers == [2]


def test_extracted_pdf_table_reaches_the_chunks_row_wise(tmp_path: Path) -> None:
    path = write_sample(tmp_path, table_pdf_bytes(), "table.pdf")
    result = an_extractor().extract(path, media_type=PDF_MEDIA_TYPE)

    joined = "\n".join(c.text for c in result.chunks)
    assert "Year | Revenue | Laba" in joined
    assert "2024, Revenue = 1000" in joined
    assert "2025, Laba = 360" in joined


def a_borderless_result(tmp_path: Path, **kwargs: Any) -> ExtractionResult:
    extract_options = {"chunk_max_tokens": kwargs.pop("chunk_max_tokens", 700)}
    path = write_sample(tmp_path, borderless_table_pdf_bytes(**kwargs), "borderless.pdf")
    return an_extractor(**extract_options).extract(path, media_type=PDF_MEDIA_TYPE)


def an_expected_grid(**kwargs: Any) -> list[str]:
    grid = borderless_table_cells(
        **{
            key: kwargs[key]
            for key in ("columns", "header", "rows", "ragged", "empty_cells")
            if key in kwargs
        }
    )
    return [value for line in grid for value in line if value is not None]


@pytest.mark.parametrize(
    ("columns", "header", "ragged", "empty_cells"),
    [
        (2, True, False, False),
        (5, True, False, False),
        (3, False, False, False),
        (3, True, True, False),
        (3, True, False, True),
    ],
)
def test_a_borderless_table_reaches_the_chunks_complete(
    tmp_path: Path, columns: int, header: bool, ragged: bool, empty_cells: bool
) -> None:
    kwargs = dict(
        columns=columns, header=header, rows=6, ragged=ragged, empty_cells=empty_cells
    )
    result = a_borderless_result(tmp_path, **kwargs)

    blob = "\n".join(c.text for c in result.chunks)
    missing = [value for value in an_expected_grid(**kwargs) if value not in blob]
    assert missing == [], f"cell values lost: {missing}"


def test_a_borderless_table_detected_as_a_table_repeats_the_header_in_every_part(
    tmp_path: Path,
) -> None:
    result = a_borderless_result(
        tmp_path, columns=3, header=True, rows=20, chunk_max_tokens=120
    )

    blob = "\n".join(c.text for c in result.chunks)
    if "Kolom1 |" not in blob:
        pytest.skip("the layout model did not read the synthetic grid as a table")
    parts = [c for c in result.chunks if re.search(r"R\d\dV\d", c.text)]
    assert len(parts) >= 2
    headerless = [c.text[:80] for c in parts if "Kolom1 |" not in c.text]
    assert headerless == [], f"table parts without a header line: {headerless}"


def test_a_borderless_table_keeps_row_order_in_the_chunks(tmp_path: Path) -> None:
    result = a_borderless_result(tmp_path, columns=3, header=True, rows=12)

    chunks = result.chunks
    positions = [
        next(i for i, c in enumerate(chunks) if f"R{row:02d}V1" in c.text)
        for row in range(1, 13)
    ]
    assert positions == sorted(positions)


def test_a_borderless_table_continuing_across_pages_keeps_rows_and_page_numbers(
    tmp_path: Path,
) -> None:
    kwargs: dict[str, Any] = dict(columns=3, header=True, rows=30, pages=2)
    result = a_borderless_result(tmp_path, **kwargs)

    blob = "\n".join(c.text for c in result.chunks)
    missing = [value for value in an_expected_grid(**kwargs) if value not in blob]
    assert missing == []
    assert any(2 in c.page_numbers for c in result.chunks)
    assert all(c.page_numbers in ([1], [2], [1, 2]) for c in result.chunks)


def test_when_table_detection_is_off_no_cell_value_is_lost(tmp_path: Path) -> None:
    import pymupdf4llm

    kwargs: dict[str, Any] = dict(columns=3, header=True, rows=8)
    path = write_sample(tmp_path, borderless_table_pdf_bytes(**kwargs), "borderless.pdf")
    pymupdf4llm.use_layout(False)
    try:
        result = an_extractor().extract(path, media_type=PDF_MEDIA_TYPE)
    finally:
        pymupdf4llm.use_layout(True)

    blob = "\n".join(c.text for c in result.chunks)
    missing = [value for value in an_expected_grid(**kwargs) if value not in blob]
    assert missing == [], f"values lost without table detection: {missing}"


def test_a_single_column_table_flattens_to_values() -> None:
    chunks = a_chunker().chunk(
        [block("table", "|Total|\n|---|\n|Alpha|\n|Beta|")]
    )

    joined = "\n".join(c.text for c in chunks)
    assert "Total" in joined
    assert "Alpha" in joined
    assert "Beta" in joined
    assert "|" not in joined


def test_a_label_only_row_survives_serialization() -> None:
    text = row_wise_table_text(
        ["Item", "Value"],
        [["Revenue:", "", ""]],
        has_header=True,
    )

    assert "Revenue:" in text.splitlines()


def test_the_document_artifact_is_the_markdown_itself(tmp_path: Path) -> None:
    import json

    path = write_sample(tmp_path, landmark_pdf_bytes(), "landmark.pdf")
    result = an_extractor().extract(path, media_type=PDF_MEDIA_TYPE)

    payload = json.loads(result.document_json)
    assert payload["format"] == "retriv-markdown-1"
    assert payload["extractor"] == EXTRACTOR_VERSION
    assert "Microsoft develops and licenses software." in payload["markdown"]
    assert payload["page_count"] == 2


def test_an_embedded_picture_is_lifted_and_marked(tmp_path: Path) -> None:
    path = write_sample(tmp_path, image_pdf_bytes(), "image.pdf")
    result = an_extractor(picture_images_enabled=True).extract(path, media_type=PDF_MEDIA_TYPE)

    assert len(result.images) == 1
    image = result.images[0]
    assert image.page_number == 1
    assert image.media_type == "image/png"
    assert image.data.startswith(b"\x89PNG")
    assert image.describe_hint == "picture"
    assert any(picture_refs(c.text) == [image.ref] for c in result.chunks)


def test_a_full_page_image_is_read_as_a_scan_not_a_picture(tmp_path: Path) -> None:
    path = write_sample(tmp_path, image_pdf_bytes(full_page=True), "fullpage.pdf")
    result = an_extractor(
        picture_images_enabled=True,
        scan_page_describe=False,
    ).extract(path, media_type=PDF_MEDIA_TYPE)

    assert result.images == []


def test_the_image_caps_bound_what_is_collected(tmp_path: Path) -> None:
    path = write_sample(tmp_path, image_pdf_bytes(), "image.pdf")
    result = an_extractor(
        picture_images_enabled=True,
        max_image_bytes=10,
    ).extract(path, media_type=PDF_MEDIA_TYPE)

    assert result.images == []
    assert not any(picture_refs(c.text) for c in result.chunks)


def test_a_vector_chart_is_rendered_and_marked(tmp_path: Path) -> None:
    """Line-art is a picture even though the source holds no image.

    A 10-K's stock-performance graph is drawn as paths: nothing in the PDF
    declares it an image, so ``get_image_info`` cannot see it and the curve's
    shape never reaches a reader. Clustering the line-art is what recovers it.
    """
    path = write_sample(tmp_path, vector_chart_pdf_bytes(), "chart.pdf")
    result = an_extractor(picture_images_enabled=True).extract(path, media_type=PDF_MEDIA_TYPE)

    assert len(result.images) == 1
    image = result.images[0]
    assert image.ref == "chart-p1-0"
    assert image.page_number == 1
    assert image.media_type == "image/png"
    assert image.data.startswith(b"\x89PNG")
    assert image.width > 0 and image.height > 0
    assert any(picture_refs(c.text) == [image.ref] for c in result.chunks)


def test_a_ruled_table_is_not_mistaken_for_a_chart(tmp_path: Path) -> None:
    """A table's grid is line-art too, and admitting it is the expensive error.

    Every cell holds text and no line runs diagonally, which is exactly the
    combination the score exists to reject.
    """
    path = write_sample(tmp_path, vector_table_pdf_bytes(), "table.pdf")
    result = an_extractor(picture_images_enabled=True).extract(path, media_type=PDF_MEDIA_TYPE)

    assert [image for image in result.images if image.ref.startswith("chart-")] == []
    assert not any(picture_refs(c.text) for c in result.chunks)


def test_vector_chart_detection_is_off_with_image_processing(tmp_path: Path) -> None:
    path = write_sample(tmp_path, vector_chart_pdf_bytes(), "chart.pdf")
    result = an_extractor(picture_images_enabled=False).extract(path, media_type=PDF_MEDIA_TYPE)

    assert result.images == []


def test_a_page_with_a_lifted_picture_is_not_searched_for_charts(tmp_path: Path) -> None:
    """One picture per shape: a bitmap page is not re-drawn as line-art."""
    path = write_sample(tmp_path, image_pdf_bytes(), "image.pdf")
    result = an_extractor(picture_images_enabled=True).extract(path, media_type=PDF_MEDIA_TYPE)

    assert len(result.images) == 1
    assert result.images[0].describe_hint == "picture"


def test_a_bar_chart_is_found_without_a_single_diagonal(tmp_path: Path) -> None:
    """A bar chart is all verticals: the trend-line test alone would miss it.

    Its value labels also make the interior denser than a line chart's, so the
    density gate pushes it down too. The bar signal is what recovers it, and
    without it five charts on the annual-report corpus were lost.
    """
    path = write_sample(tmp_path, vector_bar_chart_pdf_bytes(), "bars.pdf")
    result = an_extractor(picture_images_enabled=True).extract(path, media_type=PDF_MEDIA_TYPE)

    charts = [image for image in result.images if image.ref.startswith("chart-")]
    assert len(charts) == 1
    assert charts[0].data.startswith(b"\x89PNG")


def test_zebra_striping_alone_does_not_make_a_chart(tmp_path: Path) -> None:
    """Alternating row fills give one width and two heights -- not enough.

    This is the shape that made a four-point bar bonus admit forty-three table
    cells; three points and a three-height floor is what holds it out.
    """
    path = write_sample(tmp_path, vector_zebra_table_pdf_bytes(), "zebra.pdf")
    result = an_extractor(picture_images_enabled=True).extract(path, media_type=PDF_MEDIA_TYPE)

    assert [image for image in result.images if image.ref.startswith("chart-")] == []


def test_an_unsupported_media_type_is_an_extraction_error(tmp_path: Path) -> None:
    from app.extraction.base import ExtractionError

    path = write_sample(tmp_path, b"nothing", "file.txt")
    with pytest.raises(ExtractionError):
        an_extractor().extract(path, media_type="text/plain")


def build_docx_sample() -> bytes:
    import docx

    document = docx.Document()
    document.add_heading("Laporan Keuangan", level=1)
    document.add_heading("Pendapatan", level=2)
    document.add_paragraph("Pendapatan naik dua belas persen.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Tahun"
    table.cell(0, 1).text = "Nilai"
    table.cell(1, 0).text = "2025"
    table.cell(1, 1).text = "1200"
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_the_docx_walk_yields_headings_tables_and_no_pages(tmp_path: Path) -> None:
    path = write_sample(tmp_path, build_docx_sample(), "sample.docx")
    result = an_extractor().extract(path, media_type=DOCX_MEDIA_TYPE)

    assert result.ocr_enabled is False
    assert result.page_count == 0
    assert result.source_text_chars == 0
    body = [c for c in result.chunks if not c.is_heading_only]
    assert any(c.section_path == ["Laporan Keuangan", "Pendapatan"] for c in body)
    joined = "\n".join(c.text for c in result.chunks)
    assert "Tahun | Nilai" in joined
    assert "2025, Nilai = 1200" in joined


def test_the_docx_walk_lifts_inline_pictures(tmp_path: Path) -> None:
    import docx
    import pymupdf

    painter = pymupdf.open()
    page = painter.new_page(width=80, height=60)
    page.draw_rect(page.rect, color=None, fill=(0.1, 0.4, 0.9))
    png = page.get_pixmap().tobytes("png")
    painter.close()

    document = docx.Document()
    document.add_heading("Grafik", level=1)
    document.add_paragraph("Berikut grafiknya.")
    document.add_picture(BytesIO(png))
    buffer = BytesIO()
    document.save(buffer)

    path = write_sample(tmp_path, buffer.getvalue(), "picture.docx")
    result = an_extractor(picture_images_enabled=True).extract(path, media_type=DOCX_MEDIA_TYPE)

    assert len(result.images) == 1
    assert result.images[0].describe_hint == "picture"
    assert any(picture_refs(c.text) for c in result.chunks)


def a_small_png() -> bytes:
    import pymupdf

    painter = pymupdf.open()
    page = painter.new_page(width=80, height=60)
    page.draw_rect(page.rect, color=None, fill=(0.1, 0.4, 0.9))
    png = page.get_pixmap().tobytes("png")
    painter.close()
    return png


def test_a_picture_inside_a_table_cell_is_lifted_after_the_table(tmp_path: Path) -> None:
    import docx

    document = docx.Document()
    document.add_heading("Grafik", level=1)
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Metrik"
    table.cell(0, 1).text = "Visual"
    table.cell(1, 0).text = "Revenue"
    run = table.cell(1, 1).paragraphs[0].add_run()
    run.add_picture(BytesIO(a_small_png()))
    document.add_paragraph("Catatan di bawah tabel.")
    buffer = BytesIO()
    document.save(buffer)

    path = write_sample(tmp_path, buffer.getvalue(), "table-picture.docx")
    result = an_extractor(picture_images_enabled=True).extract(path, media_type=DOCX_MEDIA_TYPE)

    assert len(result.images) == 1
    assert result.images[0].describe_hint == "picture"
    marker_index = next(
        i for i, c in enumerate(result.chunks) if picture_refs(c.text)
    )
    table_index = next(
        i for i, c in enumerate(result.chunks) if "Metrik | Visual" in c.text
    )
    assert marker_index >= table_index
    if marker_index == table_index:
        text = result.chunks[marker_index].text
        assert text.index("Metrik | Visual") < text.index("[[PICTURE:")
    note_index = next(
        i for i, c in enumerate(result.chunks) if "Catatan" in c.text
    )
    assert note_index >= marker_index


def test_a_picture_only_table_still_lifts_its_picture(tmp_path: Path) -> None:
    import docx

    document = docx.Document()
    table = document.add_table(rows=1, cols=1)
    run = table.cell(0, 0).paragraphs[0].add_run()
    run.add_picture(BytesIO(a_small_png()))
    buffer = BytesIO()
    document.save(buffer)

    path = write_sample(tmp_path, buffer.getvalue(), "picture-table.docx")
    result = an_extractor(picture_images_enabled=True).extract(path, media_type=DOCX_MEDIA_TYPE)

    assert len(result.images) == 1
    assert any(picture_refs(c.text) for c in result.chunks)


def test_docx_blocks_carry_order_anchors_into_chunks(tmp_path: Path) -> None:
    import docx

    document = docx.Document()
    document.add_paragraph("First paragraph.")
    document.add_paragraph("Second paragraph.")
    buffer = BytesIO()
    document.save(buffer)
    path = write_sample(tmp_path, buffer.getvalue(), "anchors.docx")

    blocks, _images = build_docx_blocks(path, image_render_scale=2.0)
    assert [block.regions[0].anchor for block in blocks] == [
        "docx-0",
        "docx-1",
    ]

    result = an_extractor().extract(path, media_type=DOCX_MEDIA_TYPE)
    regions = [
        region
        for chunk in result.chunks
        for region in chunk.provenance.get("regions", [])
    ]
    assert regions == [
        {"page_no": None, "bbox": None, "kind": "docx-block", "ref": "", "anchor": "docx-0"},
        {"page_no": None, "bbox": None, "kind": "docx-block", "ref": "", "anchor": "docx-1"},
    ]


def test_docx_heading_styles_map_to_their_own_level(tmp_path: Path) -> None:
    import docx

    document = docx.Document()
    document.add_heading("Judul Utama", level=0)
    document.add_paragraph("Subjudul", style="Subtitle")
    for level in range(1, 7):
        document.add_heading(f"Bab level {level}", level=level)
    document.add_paragraph("Isi di bawah heading terdalam.")
    buffer = BytesIO()
    document.save(buffer)

    path = write_sample(tmp_path, buffer.getvalue(), "levels.docx")
    blocks, _images = build_docx_blocks(path, image_render_scale=2.0)
    levels = [(b.text, b.level) for b in blocks if b.kind == "heading"]
    assert levels == [
        ("Judul Utama", 1),
        ("Subjudul", 2),
        ("Bab level 1", 1),
        ("Bab level 2", 2),
        ("Bab level 3", 3),
        ("Bab level 4", 4),
        ("Bab level 5", 5),
        ("Bab level 6", 6),
    ]

    result = an_extractor().extract(path, media_type=DOCX_MEDIA_TYPE)
    body = [c for c in result.chunks if not c.is_heading_only]
    assert body[0].section_path == [
        "Bab level 1",
        "Bab level 2",
        "Bab level 3",
        "Bab level 4",
        "Bab level 5",
        "Bab level 6",
    ]


def test_docx_keeps_the_written_paragraph_table_paragraph_order(tmp_path: Path) -> None:
    import docx

    document = docx.Document()
    document.add_paragraph("Paragraf pembuka sebelum tabel.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Kode"
    table.cell(0, 1).text = "Nilai"
    table.cell(1, 0).text = "A1"
    table.cell(1, 1).text = "77"
    document.add_paragraph("Paragraf penutup setelah tabel.")
    buffer = BytesIO()
    document.save(buffer)

    path = write_sample(tmp_path, buffer.getvalue(), "interleaved.docx")
    result = an_extractor().extract(path, media_type=DOCX_MEDIA_TYPE)

    blob = "\n".join(c.text for c in result.chunks)
    assert blob.index("Paragraf pembuka") < blob.index("Kode | Nilai")
    assert blob.index("Kode | Nilai") < blob.index("Paragraf penutup")


def test_a_long_docx_table_splits_at_rows_repeating_the_header(tmp_path: Path) -> None:
    import docx

    document = docx.Document()
    table = document.add_table(rows=21, cols=2)
    table.cell(0, 0).text = "Baris"
    table.cell(0, 1).text = "Angka"
    for row in range(1, 21):
        table.cell(row, 0).text = f"R{row:02d}"
        table.cell(row, 1).text = str(row * 10)
    buffer = BytesIO()
    document.save(buffer)

    path = write_sample(tmp_path, buffer.getvalue(), "long-table.docx")
    result = an_extractor(chunk_max_tokens=120).extract(path, media_type=DOCX_MEDIA_TYPE)

    blob = "\n".join(c.text for c in result.chunks)
    for row in range(1, 21):
        assert f"R{row:02d}" in blob
    parts = [c for c in result.chunks if re.search(r"R\d\d", c.text)]
    assert len(parts) >= 2
    headerless = [c.text[:80] for c in parts if "Baris | Angka" not in c.text]
    assert headerless == [], f"table parts without a header line: {headerless}"


def test_docx_merged_cells_keep_every_value(tmp_path: Path) -> None:
    import docx

    document = docx.Document()
    table = document.add_table(rows=3, cols=2)
    table.cell(0, 0).text = "Nama"
    table.cell(0, 1).text = "Nilai"
    merged = table.cell(1, 0).merge(table.cell(1, 1))
    merged.text = "Sel gabungan lebar"
    table.cell(2, 0).text = "C2"
    table.cell(2, 1).text = "31"
    buffer = BytesIO()
    document.save(buffer)

    path = write_sample(tmp_path, buffer.getvalue(), "merged.docx")
    result = an_extractor().extract(path, media_type=DOCX_MEDIA_TYPE)

    blob = "\n".join(c.text for c in result.chunks)
    for value in ("Nama", "Nilai", "Sel gabungan lebar", "C2", "31"):
        assert value in blob, f"merged-cell value lost: {value}"


def test_an_unknown_docx_style_stays_a_text_block(tmp_path: Path) -> None:
    import docx
    from docx.enum.style import WD_STYLE_TYPE

    document = docx.Document()
    style = document.styles.add_style("Kustom Kita", WD_STYLE_TYPE.PARAGRAPH)
    document.add_paragraph("Ini Heading palsu", style=style)
    document.add_paragraph("Isi biasa.")
    buffer = BytesIO()
    document.save(buffer)

    path = write_sample(tmp_path, buffer.getvalue(), "custom-style.docx")
    result = an_extractor().extract(path, media_type=DOCX_MEDIA_TYPE)

    assert all(
        c.section_path == [] or not c.is_heading_only for c in result.chunks
    )
    blob = "\n".join(c.text for c in result.chunks)
    assert "Ini Heading palsu" in blob
    assert not any(c.is_heading_only for c in result.chunks)


def test_the_docx_image_caps_bound_what_is_collected(tmp_path: Path) -> None:
    import docx

    document = docx.Document()
    document.add_paragraph("Ada gambar di bawah.")
    document.add_picture(BytesIO(a_small_png()))
    buffer = BytesIO()
    document.save(buffer)

    path = write_sample(tmp_path, buffer.getvalue(), "capped.docx")
    result = an_extractor(picture_images_enabled=True, max_image_bytes=10).extract(
        path, media_type=DOCX_MEDIA_TYPE
    )

    assert result.images == []
    assert not any(picture_refs(c.text) for c in result.chunks)


def test_a_picture_in_a_nested_table_is_still_lifted(tmp_path: Path) -> None:
    import docx

    document = docx.Document()
    outer = document.add_table(rows=1, cols=1)
    inner = outer.cell(0, 0).add_table(rows=1, cols=1)
    run = inner.cell(0, 0).paragraphs[0].add_run()
    run.add_picture(BytesIO(a_small_png()))
    buffer = BytesIO()
    document.save(buffer)

    path = write_sample(tmp_path, buffer.getvalue(), "nested.docx")
    result = an_extractor(picture_images_enabled=True).extract(path, media_type=DOCX_MEDIA_TYPE)

    assert len(result.images) == 1
    assert any(picture_refs(c.text) for c in result.chunks)


def test_a_two_column_pdf_reads_each_column_whole(tmp_path: Path) -> None:
    path = write_sample(tmp_path, two_column_pdf_bytes(), "twocol.pdf")
    result = an_extractor().extract(path, media_type=PDF_MEDIA_TYPE)

    blob = "\n".join(c.text for c in result.chunks)
    lefts = [blob.index(f"L{index:02d}") for index in range(16)]
    rights = [blob.index(f"R{index:02d}") for index in range(16)]
    assert lefts == sorted(lefts)
    assert rights == sorted(rights)
    assert max(lefts) < min(rights), "the columns were interleaved in the chunks"


def test_a_docx_text_box_is_read_after_its_anchor(tmp_path: Path) -> None:
    import docx
    from docx.oxml import parse_xml

    document = docx.Document()
    document.add_paragraph("Sebelum sidebar.")
    anchor = document.add_paragraph()
    pict = parse_xml(
        '<w:pict xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:v="urn:schemas-microsoft-com:vml">'
        '<v:shape style="width:100pt;height:50pt">'
        "<v:textbox><w:txbxContent>"
        "<w:p><w:r><w:t>TEKS SIDEBAR</w:t></w:r></w:p>"
        "</w:txbxContent></v:textbox>"
        "</v:shape>"
        "</w:pict>"
    )
    anchor.add_run()._r.append(pict)
    document.add_paragraph("Sesudah sidebar.")
    buffer = BytesIO()
    document.save(buffer)

    path = write_sample(tmp_path, buffer.getvalue(), "sidebar.docx")
    result = an_extractor().extract(path, media_type=DOCX_MEDIA_TYPE)

    blob = "\n".join(c.text for c in result.chunks)
    assert "TEKS SIDEBAR" in blob
    assert blob.index("Sebelum sidebar") < blob.index("TEKS SIDEBAR")
    assert blob.index("TEKS SIDEBAR") < blob.index("Sesudah sidebar")


@pytest.mark.integration
def test_a_scanned_page_is_read_by_ocr(tmp_path: Path) -> None:
    path = write_sample(tmp_path, scanned_pdf_bytes(), "scan.pdf")
    result = an_extractor().extract(path, media_type=PDF_MEDIA_TYPE)

    assert result.page_count == 1
    assert result.source_text_chars == 0
    joined = " ".join(c.text for c in result.chunks)
    assert "Pasal" in joined
    assert "arsip" in joined.lower()
    body = [c for c in result.chunks if not c.is_heading_only]
    assert body and body[0].section_path == ["Pasal 16"], (
        "a landmark on a scanned page must open its own section, not spill "
        "into whatever came before"
    )


def test_ocr_noise_never_becomes_a_heading() -> None:
    """OCR mangles words in ways the landmark patterns must not forgive."""
    promoted = promote_landmarks(
        [
            "pasal 16",  # clean: the one shape that earns promotion
            "p a sal 16 arsip",  # split letters fail the pattern
            "arsip wajib disimpan di Item 7",  # landmark not at line start
            "Item untuk rapat",  # no number after the keyword
            "x" * 200 + " Pasal 1",  # too long to be a heading
        ]
    )

    assert promoted == [
        "## pasal 16",
        "p a sal 16 arsip",
        "arsip wajib disimpan di Item 7",
        "Item untuk rapat",
        "x" * 200 + " Pasal 1",
    ]


@pytest.mark.integration
def test_the_sandbox_runs_the_native_runner(tmp_path: Path) -> None:
    from app.extraction.sandbox import SandboxedExtractor

    source = write_sample(tmp_path, landmark_pdf_bytes(), "source.pdf")
    sandbox = SandboxedExtractor(
        chunk_max_tokens=700,
        tokenizer_name="cl100k_base",
        scan_text_threshold=32,
        scan_page_describe=False,
        timeout_seconds=120,
        memory_limit_bytes=2 * 1024 * 1024 * 1024,
        max_processes=4096,
        max_artifact_bytes=10 * 1024 * 1024,
        max_chunk_payload_bytes=10 * 1024 * 1024,
        max_extracted_text_bytes=5 * 1024 * 1024,
        max_chunks=100,
        max_image_bytes=5 * 1024 * 1024,
        max_total_image_bytes=50 * 1024 * 1024,
    )
    result = sandbox.extract(source, media_type=PDF_MEDIA_TYPE)

    assert result.extractor_version == EXTRACTOR_VERSION
    assert result.ocr_enabled is True
    body = [c for c in result.chunks if not c.is_heading_only]
    assert body and "Microsoft develops and licenses software." in body[0].text


def a_large_jpeg_photo(width: int = 3000, height: int = 2000) -> bytes:
    import numpy as np
    import pymupdf

    rng = np.random.default_rng(13)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height), False)
    pix.samples_mv[:] = rng.integers(
        0, 256, size=(height, width, 3), dtype=np.uint8
    ).tobytes()
    return pix.tobytes("jpeg", jpg_quality=95)


def test_an_oversized_docx_picture_is_shrunk_not_dropped(tmp_path: Path) -> None:
    """A picture over the byte cap must still reach the vision stage.

    Dropping it left the document looking complete with the figure missing
    entirely: no image, no marker, no row. Shrinking keeps the content.
    """
    import docx

    photo = a_large_jpeg_photo()
    cap = 512 * 1024
    assert len(photo) > cap, "the sample must start over the cap"

    document = docx.Document()
    document.add_paragraph("Foto tim.")
    document.add_picture(BytesIO(photo))
    buffer = BytesIO()
    document.save(buffer)
    path = write_sample(tmp_path, buffer.getvalue(), "big-photo.docx")

    result = an_extractor(
        picture_images_enabled=True,
        max_image_bytes=cap,
    ).extract(path, media_type=DOCX_MEDIA_TYPE)

    assert len(result.images) == 1, "the oversized picture must survive the cap"
    image = result.images[0]
    assert len(image.data) <= cap
    assert image.media_type == "image/jpeg", "the source format is carried over"
    assert any(picture_refs(c.text) for c in result.chunks), "a marker must exist"


def test_the_shared_resizer_lands_under_the_cap_and_keeps_the_format() -> None:
    from app.extraction.images import fit_image_to_limit, image_format_of

    photo = a_large_jpeg_photo()
    cap = 256 * 1024

    fitted = fit_image_to_limit(photo, "image/jpeg", cap, 1568)

    assert fitted is not None
    data, media_type = fitted
    assert len(data) <= cap
    assert media_type == "image/jpeg"
    assert image_format_of(data) == b"jpeg"


def test_the_shared_resizer_leaves_a_small_picture_untouched() -> None:
    import pymupdf

    from app.extraction.images import fit_image_to_limit

    painter = pymupdf.open()
    page = painter.new_page(width=120, height=90)
    page.draw_rect(page.rect, color=None, fill=(0.2, 0.4, 0.9))
    small = page.get_pixmap().tobytes("png")
    painter.close()

    fitted = fit_image_to_limit(small, "image/png", 5 * 1024 * 1024, 1568)

    assert fitted is not None
    assert fitted[0] is small, "an in-limits picture must not be re-encoded"


def test_an_oversized_pdf_crop_is_shrunk_not_dropped(tmp_path: Path) -> None:
    """The PDF path re-renders smaller instead of discarding the figure."""
    path = write_sample(tmp_path, image_pdf_bytes(), "image.pdf")

    result = an_extractor(
        picture_images_enabled=True,
        max_image_bytes=2_000,
    ).extract(path, media_type=PDF_MEDIA_TYPE)

    assert result.images, "a capped crop must still produce a picture"
    assert all(len(image.data) <= 2_000 for image in result.images)


@pytest.mark.integration
def test_a_deck_is_rendered_to_pdf_and_its_text_stays_text(tmp_path: Path) -> None:
    """A slide deck reaches the pipeline as a PDF, and its words stay words.

    Rendering is what turns a native chart from a recipe into a picture the
    PDF path can find and describe; the text layer survives the conversion,
    so a deck is still searchable rather than one image per slide.
    """
    import shutil as _shutil

    from app.documents.conversion import convert_to_pdf

    if _shutil.which("soffice") is None and _shutil.which("libreoffice") is None:
        pytest.skip("LibreOffice is not installed here; the worker image has it")

    source = write_sample(tmp_path, pptx_bytes(), "deck.pptx")

    rendered = convert_to_pdf(source)

    import pymupdf

    with pymupdf.open(rendered) as pdf:
        assert pdf.page_count == 3, "one page per slide"
        text = "\n".join(page.get_text() for page in pdf)
    assert "Laporan Kuartal Empat" in text
    assert "Pendapatan naik dua belas persen" in text
    assert "Tren Kuartalan" in text, "a native chart's labels are still text"
    assert "Q1" in text
