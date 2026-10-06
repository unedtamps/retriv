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
    body = rows if rows is not None else (
        "id,status,amount,currency\n"
        "1001,OPEN,1500.00,USD\n"
        "1002,CLOSED,220.50,USD\n"
        "1003,OPEN,9800.00,EUR\n"
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
