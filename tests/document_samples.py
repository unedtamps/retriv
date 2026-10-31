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


def scanned_pdf_bytes() -> bytes:
    """A page that is only pixels: the text has no layer to read."""
    rendered = pymupdf.open(stream=landmark_pdf_bytes(), filetype="pdf")
    pixmap = rendered[1].get_pixmap(dpi=150)
    rendered.close()
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_image(page.rect, pixmap=pixmap)
    return document.tobytes()
