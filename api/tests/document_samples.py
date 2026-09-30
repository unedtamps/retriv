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


def xlsx_bytes() -> bytes:
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
