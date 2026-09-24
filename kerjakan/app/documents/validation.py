import hashlib
import os
import struct
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from functools import partial
from pathlib import Path, PurePosixPath
from xml.etree.ElementTree import Element

import pymupdf
from anyio import to_thread
from defusedxml.ElementTree import fromstring
from fastapi import UploadFile

from app.core.config import Settings
from app.core.errors import AppError

PDF_MEDIA_TYPE = "application/pdf"
DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".xlsx"}
OFFICE_CFB_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


@dataclass(frozen=True)
class StagedUpload:
    path: Path
    original_filename: str
    extension: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ValidationResult:
    media_type: str
    page_count: int | None = None


async def stage_upload(upload: UploadFile, settings: Settings) -> StagedUpload:
    filename = sanitize_filename(upload.filename)
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise AppError(
            status_code=415,
            code="unsupported_file_type",
            title="Unsupported file type",
            detail="Only PDF, DOCX, and XLSX files are accepted.",
        )
    if upload.size is not None and upload.size > settings.max_upload_bytes:
        raise file_too_large_error(settings.max_upload_bytes)

    settings.upload_temp_dir.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix="document-", suffix=extension, dir=settings.upload_temp_dir
    )
    temporary_path = Path(temporary_name)
    digest = hashlib.sha256()
    total_bytes = 0

    try:
        with os.fdopen(file_descriptor, "wb") as destination:
            while chunk := await upload.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > settings.max_upload_bytes:
                    raise file_too_large_error(settings.max_upload_bytes)
                digest.update(chunk)
                destination.write(chunk)
        if total_bytes == 0:
            raise AppError(
                status_code=422,
                code="empty_file",
                title="Empty file",
                detail="The uploaded file is empty.",
            )
        return StagedUpload(
            path=temporary_path,
            original_filename=filename,
            extension=extension,
            size_bytes=total_bytes,
            sha256=digest.hexdigest(),
        )
    except Exception:
        await to_thread.run_sync(partial(temporary_path.unlink, missing_ok=True))
        raise


def sanitize_filename(filename: str | None) -> str:
    if not filename:
        raise AppError(
            status_code=422,
            code="missing_filename",
            title="Missing filename",
            detail="The upload must include a filename.",
        )

    normalized = unicodedata.normalize("NFC", filename).replace("\\", "/")
    basename = normalized.rsplit("/", maxsplit=1)[-1]
    cleaned = "".join(
        character
        for character in basename
        if unicodedata.category(character) not in {"Cc", "Cf"}
    )
    cleaned = cleaned.strip(" .")
    if not cleaned or len(cleaned.encode("utf-8")) > 255:
        raise AppError(
            status_code=422,
            code="invalid_filename",
            title="Invalid filename",
            detail="The filename is empty or longer than 255 bytes.",
        )
    return cleaned


def validate_document(staged: StagedUpload, settings: Settings) -> ValidationResult:
    if staged.extension == ".pdf":
        return validate_pdf(staged.path, settings.max_pdf_pages)
    return validate_ooxml(staged.path, staged.extension, settings)


def validate_pdf(path: Path, max_pages: int) -> ValidationResult:
    with path.open("rb") as source:
        if b"%PDF-" not in source.read(1024):
            raise invalid_document_error("The file content is not a PDF document.")

    try:
        with pymupdf.open(path) as document:
            if not document.is_pdf:
                raise invalid_document_error("The file content is not a PDF document.")
            if document.needs_pass or document.metadata.get("encryption"):
                raise encrypted_document_error()
            page_count = document.page_count
            if page_count < 1:
                raise invalid_document_error("The PDF does not contain any pages.")
            if page_count > max_pages:
                raise AppError(
                    status_code=422,
                    code="too_many_pages",
                    title="PDF page limit exceeded",
                    detail=f"PDF documents cannot contain more than {max_pages} pages.",
                )
    except AppError:
        raise
    except (
        pymupdf.FileDataError,
        pymupdf.EmptyFileError,
        RuntimeError,
        ValueError,
    ) as exc:
        raise invalid_document_error("The PDF document is corrupt or invalid.") from exc

    return ValidationResult(media_type=PDF_MEDIA_TYPE, page_count=page_count)


def validate_ooxml(path: Path, extension: str, settings: Settings) -> ValidationResult:
    with path.open("rb") as source:
        signature = source.read(len(OFFICE_CFB_SIGNATURE))
    if signature == OFFICE_CFB_SIGNATURE:
        raise encrypted_document_error()
    preflight_zip(path, settings)
    if not zipfile.is_zipfile(path):
        raise invalid_document_error("The file is not a valid Office Open XML package.")

    expected = {
        ".docx": (
            "word/document.xml",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
            DOCX_MEDIA_TYPE,
        ),
        ".xlsx": (
            "xl/workbook.xml",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
            XLSX_MEDIA_TYPE,
        ),
    }
    main_part, main_content_type, media_type = expected[extension]

    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > settings.max_archive_entries:
                raise unsafe_archive_error(
                    "The Office package contains too many entries."
                )

            names: set[str] = set()
            total_compressed = 0
            total_uncompressed = 0
            for entry in entries:
                validate_archive_entry(entry, settings)
                if entry.filename in names:
                    raise unsafe_archive_error(
                        "The Office package contains duplicate entries."
                    )
                names.add(entry.filename)
                total_compressed += entry.compress_size
                total_uncompressed += entry.file_size

            if total_uncompressed > settings.max_archive_uncompressed_bytes:
                raise unsafe_archive_error(
                    "The uncompressed Office package is too large."
                )
            denominator = max(total_compressed, 1)
            if (
                total_uncompressed / denominator
                > settings.max_archive_compression_ratio
            ):
                raise unsafe_archive_error(
                    "The Office package compression ratio is unsafe."
                )

            required_entries = {"[Content_Types].xml", "_rels/.rels", main_part}
            if not required_entries.issubset(names):
                raise invalid_document_error(
                    "The Office package is missing required content."
                )

            content_types_info = archive.getinfo("[Content_Types].xml")
            if content_types_info.file_size > 1024 * 1024:
                raise unsafe_archive_error(
                    "The Office content type manifest is too large."
                )
            main_info = archive.getinfo(main_part)
            relationships_info = archive.getinfo("_rels/.rels")
            if (
                main_info.file_size > settings.max_office_xml_bytes
                or relationships_info.file_size > settings.max_office_xml_bytes
            ):
                raise unsafe_archive_error("An Office XML part is too large.")
            content_types_xml = archive.read(content_types_info)
            main_xml = archive.read(main_info)
            relationships_xml = archive.read(relationships_info)
    except AppError:
        raise
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise invalid_document_error(
            "The Office document is corrupt or invalid."
        ) from exc

    root = parse_office_xml(content_types_xml, "content type manifest")
    parse_office_xml(main_xml, "main document part")
    parse_office_xml(relationships_xml, "package relationships")

    overrides: dict[str, str] = {}
    declared_content_types: list[str] = []
    for node in root:
        content_type = node.attrib.get("ContentType", "")
        declared_content_types.append(content_type)
        if node.tag.rsplit("}", maxsplit=1)[-1] == "Override":
            part_name = node.attrib.get("PartName")
            if part_name:
                overrides[part_name] = content_type

    all_content_types = " ".join(declared_content_types).lower()
    has_unsafe_part = any(
        "vbaproject" in name.lower() or "/activex/" in name.lower() for name in names
    )
    if (
        "macroenabled" in all_content_types
        or "vbaproject" in all_content_types
        or "activex" in all_content_types
        or has_unsafe_part
    ):
        raise AppError(
            status_code=415,
            code="macro_enabled_document",
            title="Macro-enabled document is not supported",
            detail="Macro-enabled Office documents are not accepted.",
        )
    if overrides.get(f"/{main_part}") != main_content_type:
        raise invalid_document_error(
            "The Office document type does not match its extension."
        )

    return ValidationResult(media_type=media_type)


def preflight_zip(path: Path, settings: Settings) -> None:
    file_size = path.stat().st_size
    tail_size = min(file_size, 65_557)
    with path.open("rb") as source:
        source.seek(file_size - tail_size)
        tail = source.read(tail_size)

    end_signature = b"PK\x05\x06"
    offset = tail.rfind(end_signature)
    if offset < 0 or len(tail) - offset < 22:
        raise invalid_document_error("The Office package has no valid ZIP directory.")

    (
        _,
        disk_number,
        directory_disk,
        entries_on_disk,
        total_entries,
        directory_size,
        directory_offset,
        comment_length,
    ) = struct.unpack_from("<4s4H2LH", tail, offset)
    if (
        disk_number != 0
        or directory_disk != 0
        or entries_on_disk != total_entries
        or total_entries == 0xFFFF
        or directory_size == 0xFFFFFFFF
        or directory_offset == 0xFFFFFFFF
    ):
        raise unsafe_archive_error(
            "Multi-disk and ZIP64 Office packages are not accepted."
        )
    if total_entries > settings.max_archive_entries:
        raise unsafe_archive_error("The Office package contains too many entries.")
    if directory_size > settings.max_archive_central_directory_bytes:
        raise unsafe_archive_error("The Office package directory is too large.")
    if directory_offset + directory_size > file_size:
        raise invalid_document_error("The Office package directory is invalid.")
    if offset + 22 + comment_length > len(tail):
        raise invalid_document_error("The Office package comment is invalid.")


def validate_archive_entry(entry: zipfile.ZipInfo, settings: Settings) -> None:
    name = entry.filename
    path = PurePosixPath(name)
    if (
        not name
        or name.startswith(("/", "\\"))
        or "\\" in name
        or ".." in path.parts
        or entry.flag_bits & 0x1
    ):
        raise unsafe_archive_error("The Office package contains an unsafe entry.")
    if entry.file_size > 0 and entry.compress_size == 0:
        raise unsafe_archive_error(
            "The Office package contains an invalid compressed entry."
        )
    if entry.file_size > settings.max_archive_entry_bytes:
        raise unsafe_archive_error("The Office package contains an oversized entry.")
    if (
        entry.file_size > 0
        and entry.file_size / max(entry.compress_size, 1)
        > settings.max_archive_entry_compression_ratio
    ):
        raise unsafe_archive_error(
            "The Office package contains an unsafe compressed entry."
        )


def parse_office_xml(content: bytes, description: str) -> Element:
    try:
        return fromstring(content)
    except Exception as exc:
        raise invalid_document_error(f"The Office {description} is invalid.") from exc


def file_too_large_error(max_bytes: int) -> AppError:
    return AppError(
        status_code=413,
        code="file_too_large",
        title="File too large",
        detail=f"The file cannot exceed {max_bytes} bytes.",
    )


def invalid_document_error(detail: str) -> AppError:
    return AppError(
        status_code=422,
        code="invalid_document",
        title="Invalid document",
        detail=detail,
    )


def encrypted_document_error() -> AppError:
    return AppError(
        status_code=422,
        code="encrypted_document",
        title="Encrypted document",
        detail="Password-protected or encrypted documents are not accepted.",
    )


def unsafe_archive_error(detail: str) -> AppError:
    return AppError(
        status_code=422,
        code="unsafe_archive",
        title="Unsafe Office package",
        detail=detail,
    )
