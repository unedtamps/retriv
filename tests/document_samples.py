from collections.abc import Sequence
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pymupdf


def pdf_bytes(text: str = "Quarterly report") -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    content = document.tobytes()
    document.close()
    return content


def encrypted_pdf_bytes(user_password: str = "user-password") -> bytes:
    document = pymupdf.open()
    document.new_page()
    content = document.tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256,  # type: ignore[attr-defined]
        owner_pw="owner-password",
        user_pw=user_password,
    )
    document.close()
    return content


def docx_bytes() -> bytes:
    content_types = b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    return office_archive(
        content_types,
        "word/document.xml",
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
    )


def csv_bytes(rows: str | None = None) -> bytes:
    body = (
        rows
        if rows is not None
        else (
            "id,status,amount,currency\n"
            "1001,OPEN,1500.00,USD\n"
            "1002,CLOSED,220.50,USD\n"
            "1003,OPEN,9800.00,EUR\n"
        )
    )
    return body.encode("utf-8")


def csv_with_bom_bytes() -> bytes:
    """A UTF-8 BOM, which Excel writes and which plain utf-8 rejects."""
    return b"\xef\xbb\xbf" + csv_bytes()


def csv_semicolon_bytes() -> bytes:
    return b"id;status;amount\n1;OPEN;10\n2;CLOSED;20\n"


def csv_ragged_bytes() -> bytes:
    return b"id,status,amount\n1,OPEN\n"


def csv_header_only_bytes() -> bytes:
    return b"id,status,amount\n"


def csv_empty_bytes() -> bytes:
    return b""


def csv_binary_bytes() -> bytes:
    return b"id,status\n\x00\x01\x02,OPEN\n"


def malformed_docx_bytes() -> bytes:
    content_types = b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    return office_archive(content_types, "word/document.xml", b"<not-closed")


def macro_default_docx_bytes() -> bytes:
    content_types = b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="bin" ContentType="application/vnd.ms-office.vbaProject"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    return office_archive(
        content_types,
        "word/document.xml",
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
        extras={"word/vbaProject.bin": b"not-a-real-macro"},
    )


def office_archive(
    content_types: bytes,
    main_path: str,
    main_content: bytes,
    *,
    extras: dict[str, bytes] | None = None,
) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr(
            "_rels/.rels",
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        )
        archive.writestr(main_path, main_content)
        for path, content in (extras or {}).items():
            archive.writestr(path, content)
    return output.getvalue()


def xlsx_bytes() -> bytes:
    """A valid but now-unsupported spreadsheet.

    Kept so the API can be tested refusing it. It used to be the third
    accepted upload type, which is why it still needs a sample: the
    validator accepted it and the extractor then failed, so an upload that
    returned 202 was guaranteed to break later.
    """
    content_types = b"""<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Override PartName="/xl/workbook.xml"
    ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
</Types>"""
    return office_archive(
        content_types,
        "xl/workbook.xml",
        b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>',
    )


def landmark_pdf_bytes() -> bytes:
    """Two pages whose headings are landmarks set in body type."""
    document = pymupdf.open()
    first = document.new_page()
    first.insert_text((72, 72), "PART I", fontsize=11)
    first.insert_text((72, 100), "Item 1. Business", fontsize=11)
    first.insert_text((72, 128), "Microsoft develops and licenses software.", fontsize=11)
    first.insert_text((72, 156), "Item 1A. Risk Factors", fontsize=11)
    first.insert_text((72, 184), "Competition may reduce software margins.", fontsize=11)
    second = document.new_page()
    second.insert_text((72, 72), "Pasal 16", fontsize=11)
    second.insert_text((72, 100), "Setiap pemilik dokumen wajib menyimpan arsip.", fontsize=11)
    return document.tobytes()


def table_pdf_bytes() -> bytes:
    """One page holding a ruled three-by-three table."""
    document = pymupdf.open()
    page = document.new_page()
    columns = (100, 233, 366, 500)
    rows = (100, 140, 180, 220)
    for x in columns:
        page.draw_line((x, rows[0]), (x, rows[-1]))
    for y in rows:
        page.draw_line((columns[0], y), (columns[-1], y))
    cells = {
        (0, 0): "Year",
        (0, 1): "Revenue",
        (0, 2): "Laba",
        (1, 0): "2024",
        (1, 1): "1000",
        (1, 2): "300",
        (2, 0): "2025",
        (2, 1): "1200",
        (2, 2): "360",
    }
    for (row, column), text in cells.items():
        page.insert_text(
            (columns[column] + 6, rows[row] + 26),
            text,
            fontsize=11,
        )
    return document.tobytes()


def borderless_table_cells(
    *,
    columns: int,
    header: bool,
    rows: int,
    ragged: bool = False,
    empty_cells: bool = False,
) -> list[list[str | None]]:
    """The exact cell grid :func:`borderless_table_pdf_bytes` writes.

    ``None`` means a blank cell. Tests read this grid to know what to
    expect from extraction without duplicating the value rules.
    """
    grid: list[list[str | None]] = []
    if header:
        grid.append([f"Kolom{index + 1}" for index in range(columns)])
    for row in range(1, rows + 1):
        line: list[str | None] = []
        for column in range(1, columns + 1):
            value: str | None = f"R{row:02d}V{column}"
            if ragged:
                value = f"{value}{' x' * (column % 3)}"
            if empty_cells and column > 1 and row % 3 == 0:
                value = None
            line.append(value)
        grid.append(line)
    return grid


def borderless_table_pdf_bytes(
    *,
    columns: int = 3,
    header: bool = True,
    rows: int = 6,
    ragged: bool = False,
    pages: int = 1,
    empty_cells: bool = False,
) -> bytes:
    """A table of aligned text columns with no drawn lines at all.

    The shape the Wdesk financial statements take: columns stand where the
    whitespace puts them, so only a layout model can see the grid. Callers
    keep ``rows`` small enough for ``pages`` (roughly 23 rows per page) --
    rows past the last page would land outside the page bounds.
    """
    grid = borderless_table_cells(
        columns=columns,
        header=header,
        rows=rows,
        ragged=ragged,
        empty_cells=empty_cells,
    )
    document = pymupdf.open()
    step = 450 / columns
    xs = [72 + index * step for index in range(columns)]
    row_height = 26
    page = document.new_page()
    page.insert_text((72, 72), "Quarterly figures", fontsize=11)
    page_index = 0
    y = 100
    for line in grid:
        if y > 750 and page_index < pages - 1:
            page = document.new_page()
            page_index += 1
            y = 72
        for column, value in enumerate(line):
            if value is not None:
                page.insert_text((xs[column], y), value, fontsize=11)
        y += row_height
    return document.tobytes()


def two_column_pdf_bytes(*, lines: int = 16) -> bytes:
    """A two-column page whose words were written interleaved.

    The content stream order is L0 R0 L1 R1 ..., the worst case: only a
    reading-order model can return each column whole. Keep ``lines`` at 33
    or below so both columns fit the page.
    """
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Artikel Dua Kolom", fontsize=14)
    for index in range(lines):
        y = 110 + index * 20
        page.insert_text((72, y), f"L{index:02d} kalimat kolom kiri urutan {index}.")
        page.insert_text((330, y), f"R{index:02d} kalimat kolom kanan urutan {index}.")
    return document.tobytes()


def image_pdf_bytes(*, full_page: bool = False) -> bytes:
    """One page with an embedded raster picture, or a page-covering one."""
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Item 2. Properties", fontsize=11)
    page.insert_text((72, 100), "The company owns its headquarters building.", fontsize=11)
    source = pymupdf.open()
    size = (612, 792) if full_page else (200, 150)
    painter = source.new_page(width=size[0], height=size[1])
    painter.draw_rect(painter.rect, color=None, fill=(0.8, 0.2, 0.2))
    pixmap = painter.get_pixmap()
    target = page.rect if full_page else pymupdf.Rect(300, 300, 480, 415)
    page.insert_image(target, pixmap=pixmap)
    source.close()
    return document.tobytes()


def vector_chart_pdf_bytes(*, dashed_series: bool = True) -> bytes:
    """A page whose chart is line-art, with no bitmap anywhere on it.

    This is the shape the extractor used to miss: axis rules plus polylines
    drawn as paths. Nothing in the source marks it as a picture, so only
    clustering the line-art can find it. The labels are real text, which is
    what keeps the region from looking like a table.
    """
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 60), "COMPARISON OF 5-YEAR CUMULATIVE TOTAL RETURN", fontsize=9)
    page.draw_line((72, 120), (72, 380), width=0.5)
    page.draw_line((72, 380), (540, 380), width=0.5)
    for value in (100, 200, 300):
        y = 380 - (value - 0) * 0.8
        page.draw_line((72, y), (540, y), width=0.3)
        page.insert_text((40, y + 3), f"${value}", fontsize=7)
    for index, label in enumerate(("9/25/20", "9/25/21", "9/24/22", "9/30/23")):
        page.insert_text((72 + index * 120, 395), label, fontsize=7)
    points = [(100, 380), (220, 340), (340, 355), (460, 300), (530, 260)]
    for start, end in zip(points, points[1:], strict=False):
        page.draw_line(start, end, width=1.4)
    if dashed_series:
        other = [(100, 380), (220, 320), (340, 330), (460, 280), (530, 230)]
        for start, end in zip(other, other[1:], strict=False):
            page.draw_line(start, end, width=1.2, dashes="[4 3] 0")
        page.insert_text((400, 415), "S&P 500 Index", fontsize=7)
    page.insert_text((72, 430), "Apple Inc.", fontsize=7)
    return document.tobytes()


def vector_table_pdf_bytes() -> bytes:
    """A ruled table: the cluster shape a chart filter must not claim.

    Grid lines only, and every cell carries text, so the density signal and
    the absence of a diagonal both push it away from being a picture.
    """
    document = pymupdf.open()
    page = document.new_page()
    columns = [72, 200, 330, 460, 540]
    rows = [120 + index * 22 for index in range(9)]
    for y in rows:
        page.draw_line((columns[0], y), (columns[-1], y), width=0.4)
    for x in columns:
        page.draw_line((x, rows[0]), (x, rows[-1]), width=0.4)
    for down, y in enumerate(rows[:-1]):
        for across, x in enumerate(columns[:-1]):
            page.insert_text((x + 4, y + 14), f"Row {down} Col {across}", fontsize=7)
    return document.tobytes()


def vector_bar_chart_pdf_bytes() -> bytes:
    """A bar chart: no diagonal anywhere, and a value label above each bar.

    It fails every signal a line chart passes -- there is nothing diagonal to
    find, and the labels make the interior denser than a table's rules would
    leave it -- so it is the case the bar signal exists for.
    """
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 60), "Deposits and Client Assets", fontsize=9)
    page.draw_line((72, 380), (540, 380), width=0.5)
    heights = [40, 62, 85, 108, 140, 175]
    for index, height in enumerate(heights):
        x = 90 + index * 70
        page.draw_rect(
            pymupdf.Rect(x, 380 - height, x + 44, 380),
            color=None,
            fill=(0.1, 0.2, 0.45),
        )
        page.insert_text((x + 2, 374 - height), f"${height}", fontsize=6)
    for index, label in enumerate(("2020", "2021", "2022", "2023", "2024", "2025")):
        page.insert_text((92 + index * 70, 395), label, fontsize=6)
    return document.tobytes()


def vector_zebra_table_pdf_bytes() -> bytes:
    """A table with alternating row shading, the shape most likely to pass as bars.

    Alternating fills give rectangles of one width and two heights, so the bar
    signal alone would fire. What keeps it out is that two heights is fewer
    than the three distinct heights bars require.
    """
    document = pymupdf.open()
    page = document.new_page()
    rows = [120 + index * 24 for index in range(9)]
    for index, y in enumerate(rows[:-1]):
        if index % 2 == 0:
            stripe = pymupdf.Rect(72, y, 540, rows[index + 1])
            page.draw_rect(stripe, color=None, fill=(0.9, 0.9, 0.9))
        page.insert_text((78, y + 15), f"Line item {index} with a value", fontsize=7)
    for x in (72, 300, 540):
        page.draw_line((x, rows[0]), (x, rows[-1]), width=0.4)
    for y in rows:
        page.draw_line((72, y), (540, y), width=0.4)
    return document.tobytes()


def scanned_pdf_bytes() -> bytes:
    """A page that is only pixels: the text has no layer to read."""
    rendered = pymupdf.open(stream=landmark_pdf_bytes(), filetype="pdf")
    pixmap = rendered[1].get_pixmap(dpi=150)
    rendered.close()
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_image(page.rect, pixmap=pixmap)
    return document.tobytes()

def pptx_bytes(slides: int = 3) -> bytes:
    """A deck with a title, bullets, a table and a native chart."""
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    presentation = Presentation()
    title = presentation.slides.add_slide(presentation.slide_layouts[0])
    title.shapes.title.text = "Laporan Kuartal Empat"
    title.placeholders[1].text = "Disusun oleh Tim Keuangan"

    if slides >= 2:
        body = presentation.slides.add_slide(presentation.slide_layouts[1])
        body.shapes.title.text = "Ringkasan Pendapatan"
        frame = body.placeholders[1].text_frame
        for line in ("Pendapatan naik dua belas persen", "Margin kotor empat puluh persen"):
            paragraph = frame.add_paragraph()
            paragraph.text = line
        table = body.shapes.add_table(2, 2, Inches(1), Inches(5), Inches(6), Inches(1)).table
        table.cell(0, 0).text = "Tahun"
        table.cell(0, 1).text = "Nilai"
        table.cell(1, 0).text = "2025"
        table.cell(1, 1).text = "1200"

    if slides >= 3:
        chart_slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        chart_slide.shapes.title.text = "Tren Kuartalan"
        data = CategoryChartData()
        data.categories = ["Q1", "Q2", "Q3", "Q4"]
        data.add_series("Pendapatan", (100, 140, 180, 220))
        chart_slide.shapes.add_chart(
            XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1.5), Inches(8), Inches(5), data
        )

    buffer = BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def tiny_png_bytes() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (200, 120), (30, 90, 160))
    draw = ImageDraw.Draw(image)
    draw.rectangle([15, 15, 185, 105], outline=(255, 255, 255), width=4)
    draw.text((35, 55), "REGIONAL MAP", fill=(255, 255, 0))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def xlsx_table_bytes(rows: Sequence[Sequence[object]], sheet: str = "Data") -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    active = workbook.active
    active.title = sheet
    for row in rows:
        active.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def xlsx_complex_bytes() -> bytes:
    """The hostile export: junk preamble, a two-level merged header, a prose
    sheet and a hidden one."""
    from openpyxl import Workbook

    workbook = Workbook()
    report = workbook.active
    report.title = "Laporan"
    report["A1"] = "LAPORAN PENJUALAN REGIONAL 2026"
    report["A3"] = "Diunduh: 2026-11-03"
    report["A5"] = "Bulan"
    report.merge_cells("A5:A6")
    report["B5"] = "Penjualan"
    report.merge_cells("B5:D5")
    report["E5"] = "Harga"
    report.merge_cells("E5:F5")
    report["B6"] = "Q1"
    report["C6"] = "Q2"
    report["D6"] = "Q3"
    report["E6"] = "Min"
    report["F6"] = "Maks"
    for row in [
        ("Jan", 100, 120, 150, 90, 200),
        ("Feb", 180, 160, 190, 140, 250),
        ("Mar", 150, 170, 200, 120, 260),
    ]:
        report.append(row)
    prose = workbook.create_sheet("Ringkasan")
    prose["A1"] = "Ringkasan eksekutif untuk kuartal ini."
    prose["A2"] = "Penjualan tumbuh sehat di semua region."
    secret = workbook.create_sheet("Rahasia")
    secret["A1"] = "kode"
    secret["A2"] = "X1"
    secret["A3"] = "X2"
    secret.sheet_state = "hidden"
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def xlsx_visual_bytes() -> bytes:
    """A table with an embedded picture and a native bar chart on it."""
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference
    from openpyxl.drawing.image import Image as XlImage

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    for row in [("Month", "Revenue"), ("Jan", 100), ("Feb", 180), ("Mar", 150), ("Apr", 210)]:
        sheet.append(row)
    chart = BarChart()
    chart.title = "Revenue by Month"
    data = Reference(sheet, min_col=2, min_row=1, max_row=5)
    categories = Reference(sheet, min_col=1, min_row=2, max_row=5)
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(categories)
    sheet.add_chart(chart, "D2")
    sheet.add_image(XlImage(BytesIO(tiny_png_bytes())), "A10")
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def xlsx_prose_bytes() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    prose = workbook.active
    prose.title = "Catatan"
    prose["A1"] = "Catatan rapat direksi bulan November."
    prose["A2"] = "Tidak ada tabel di berkas ini."
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
