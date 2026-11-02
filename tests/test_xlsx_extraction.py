"""The workbook path: multi-sheet rows, detected headers, described visuals.

A workbook is a folder of tables and a few pictures. These tests pin the
three contracts at once: every tabular sheet lands in the row store under
its own name, a header that took two merged rows comes out as composed
column names, and the pictures and charts a CSV cannot carry are described
by the vision stage and served as image rows -- while Qdrant stays empty,
exactly as it does for a CSV.
"""

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import (
    RUN_FAILED,
    RUN_QUEUED,
    RUN_READY,
    Document,
    DocumentImage,
    DocumentRow,
    ProcessingRun,
)
from app.documents.validation import (
    XLSX_MEDIA_TYPE,
    StagedUpload,
    validate_document,
)
from tests.conftest import SeededEntities
from tests.document_samples import (
    xlsx_complex_bytes,
    xlsx_prose_bytes,
    xlsx_table_bytes,
    xlsx_visual_bytes,
)
from tests.test_worker import FakeStorage, FakeVisionProvider, build_worker, seed_document


async def stored_rows(
    session_factory: async_sessionmaker[AsyncSession], document_id: object
) -> list[DocumentRow]:
    async with session_factory() as session:
        return list(
            (
                await session.scalars(
                    select(DocumentRow)
                    .where(DocumentRow.document_id == document_id)
                    .order_by(DocumentRow.sheet, DocumentRow.row_number)
                )
            ).all()
        )


async def test_worker_stores_xlsx_sheets_with_their_names(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    storage = FakeStorage()
    document, _ = await seed_document(
        db_session_factory,
        entities.finance.id,
        storage=storage,
        content=xlsx_complex_bytes(),
        media_type=XLSX_MEDIA_TYPE,
        filename="laporan.xlsx",
    )
    worker = build_worker(db_session_factory, settings, storage=storage)
    assert await worker.run_once() is True

    async with db_session_factory() as session:
        run = await session.get(ProcessingRun, _.id)
        assert run is not None
        assert run.status == RUN_READY
        assert run.row_count == 3
        assert run.chunk_count == 0
        assert run.csv_columns is not None
    assert run.csv_columns == [
        {
            "sheet": "Laporan",
            "columns": [
                "Bulan",
                "Penjualan / Q1",
                "Penjualan / Q2",
                "Penjualan / Q3",
                "Harga / Min",
                "Harga / Maks",
            ],
            "rows": 3,
        }
    ]
    rows = await stored_rows(db_session_factory, document.id)
    assert [row.sheet for row in rows] == ["Laporan", "Laporan", "Laporan"]
    assert rows[0].data == {
        "Bulan": "Jan",
        "Penjualan / Q1": "100",
        "Penjualan / Q2": "120",
        "Penjualan / Q3": "150",
        "Harga / Min": "90",
        "Harga / Maks": "200",
    }
    assert rows[0].id is not None


async def test_xlsx_headers_compose_merged_levels(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """The two-row header is the whole point of detection.

    ``Penjualan`` is stored once, merged over three columns, with ``Q1 Q2
    Q3`` under it; a leaf-row-only reading would name three anonymous Q1s
    and lose which quarter each number belongs to.
    """
    storage = FakeStorage()
    document, _ = await seed_document(
        db_session_factory,
        entities.finance.id,
        storage=storage,
        content=xlsx_complex_bytes(),
        media_type=XLSX_MEDIA_TYPE,
        filename="laporan.xlsx",
    )
    worker = build_worker(db_session_factory, settings, storage=storage)
    assert await worker.run_once() is True

    rows = await stored_rows(db_session_factory, document.id)
    assert set(rows[0].data) == {
        "Bulan",
        "Penjualan / Q1",
        "Penjualan / Q2",
        "Penjualan / Q3",
        "Harga / Min",
        "Harga / Maks",
    }
    assert rows[2].data["Bulan"] == "Mar"


async def test_xlsx_records_skipped_sheets_in_the_manifest(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """A prose sheet is passed over, not lost: the manifest says so.

    The workbook's verdict must be auditable -- a reader of the artifact
    can see which sheets were read, which were skipped, and why.
    """
    storage = FakeStorage()
    document, run = await seed_document(
        db_session_factory,
        entities.finance.id,
        storage=storage,
        content=xlsx_complex_bytes(),
        media_type=XLSX_MEDIA_TYPE,
        filename="laporan.xlsx",
    )
    worker = build_worker(db_session_factory, settings, storage=storage)
    assert await worker.run_once() is True

    async with db_session_factory() as session:
        stored_run = await session.get(ProcessingRun, run.id)
        assert stored_run is not None
        assert stored_run.manifest_key is not None
    manifest = json.loads(storage.objects[(settings.s3_artifact_bucket, stored_run.manifest_key)])
    assert manifest["artifact_type"] == "retriv-spreadsheet"
    assert manifest["skipped"] == [
        {"sheet": "Ringkasan", "reason": "no table found in the first 25 rows"},
        {"sheet": "Rahasia", "reason": "hidden sheet"},
    ]
    assert manifest["sheets"] == [
        {
            "sheet": "Laporan",
            "columns": [
                "Bulan",
                "Penjualan / Q1",
                "Penjualan / Q2",
                "Penjualan / Q3",
                "Harga / Min",
                "Harga / Maks",
            ],
            "header_row": 6,
        }
    ]
    del document





async def test_xlsx_visuals_are_described_and_stored(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """Pictures and charts become described image rows; vectors stay empty.

    The chart is re-rendered from the series the file references, so the
    vision stage sees the graph a reader sees. The description is stored
    beside the picture and the document's Qdrant presence stays empty,
    exactly as for a CSV.
    """
    storage = FakeStorage()
    document, run = await seed_document(
        db_session_factory,
        entities.finance.id,
        storage=storage,
        content=xlsx_visual_bytes(),
        media_type=XLSX_MEDIA_TYPE,
        filename="visual.xlsx",
    )
    vision = FakeVisionProvider()
    worker = build_worker(
        db_session_factory, settings, storage=storage, vision=vision
    )
    assert await worker.run_once() is True

    assert sorted(vision.described) == ["chart-1", "picture-1"]
    sheet_context = [context for context in vision.contexts if "Data" in context]
    assert len(sheet_context) == 2

    async with db_session_factory() as session:
        images = (
            await session.scalars(
                select(DocumentImage)
                .where(DocumentImage.document_id == document.id)
                .order_by(DocumentImage.ref)
            )
        ).all()
        stored_run = await session.get(ProcessingRun, run.id)
        assert stored_run is not None
    assert [image.ref for image in images] == ["chart-1", "picture-1"]
    for image in images:
        assert image.described is True
        assert image.description == f"A description of {image.ref}."
        assert image.page_number == 1
        assert image.bucket == settings.s3_artifact_bucket
        assert (image.bucket, image.object_key) in storage.objects
    chart = next(image for image in images if image.ref == "chart-1")
    assert chart.caption == "Revenue by Month"
    assert stored_run.status == RUN_READY


async def test_xlsx_row_cap_fails_the_run(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    storage = FakeStorage()
    _, run = await seed_document(
        db_session_factory,
        entities.finance.id,
        storage=storage,
        content=xlsx_table_bytes([("id", "amount"), ("1", "10"), ("2", "20"), ("3", "30")]),
        media_type=XLSX_MEDIA_TYPE,
        filename="rows.xlsx",
    )
    worker = build_worker(
        db_session_factory,
        settings.model_copy(update={"max_csv_rows": 2}),
        storage=storage,
    )
    assert await worker.run_once() is True

    async with db_session_factory() as session:
        stored = await session.get(ProcessingRun, run.id)
        assert stored is not None
    assert stored.status == RUN_FAILED
    assert stored.last_error is not None
    assert "more than 2 data rows" in stored.last_error


async def test_xlsx_without_tables_fails_the_run(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """A workbook of prose has nothing to store, and says so permanently."""
    storage = FakeStorage()
    _, run = await seed_document(
        db_session_factory,
        entities.finance.id,
        storage=storage,
        content=xlsx_prose_bytes(),
        media_type=XLSX_MEDIA_TYPE,
        filename="notes.xlsx",
    )
    worker = build_worker(db_session_factory, settings, storage=storage)
    assert await worker.run_once() is True

    async with db_session_factory() as session:
        stored = await session.get(ProcessingRun, run.id)
        assert stored is not None
    assert stored.status == RUN_FAILED
    assert stored.last_error is not None
    assert "no tabular sheet" in stored.last_error


async def test_xlsx_queued_with_a_supported_media_type(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    from app.processing.service import enqueue_extract

    document = Document(
        origin_workspace_id=entities.finance.id,
        original_filename="book.xlsx",
        detected_media_type=XLSX_MEDIA_TYPE,
        size_bytes=10,
        sha256="a" * 64,
        bucket="raw-documents",
        object_key="workspaces/w/objects/x/1",
    )
    async with db_session_factory() as session, session.begin():
        session.add(document)
        await session.flush()
        run = await enqueue_extract(session, document, settings)
    assert run.status == RUN_QUEUED


def staged(path: Path, filename: str) -> StagedUpload:
    import hashlib
    from pathlib import Path as PathLib

    data = PathLib(path).read_bytes()
    return StagedUpload(
        path=PathLib(path),
        original_filename=filename,
        extension=PathLib(filename).suffix.lower(),
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def write_fixture(tmp_path: Path, name: str, content: bytes) -> Path:
    target = tmp_path / name
    target.write_bytes(content)
    return target


async def test_xlsx_validation_accepts_a_workbook(
    tmp_path: Path, settings: Settings
) -> None:
    path = write_fixture(tmp_path, "book.xlsx", xlsx_table_bytes([("a", "b"), ("1", "2")]))
    result = validate_document(staged(path, "book.xlsx"), settings)
    assert result.media_type == XLSX_MEDIA_TYPE


def test_xlsx_validation_rejects_too_many_sheets(
    tmp_path: Path, settings: Settings
) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    for index in range(int(settings.max_xlsx_sheets) + 1):
        workbook.create_sheet(f"Sheet{index}")
    buffer = __import__("io").BytesIO()
    workbook.save(buffer)
    path = write_fixture(tmp_path, "many.xlsx", buffer.getvalue())
    with pytest.raises(AppError) as error:
        validate_document(staged(path, "many.xlsx"), settings)
    assert error.value.status_code == 422
    assert error.value.code == "too_many_pages"


def test_xlsx_validation_rejects_macros(
    tmp_path: Path, settings: Settings
) -> None:
    import io
    import zipfile

    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active["A1"] = "x"
    buffer = io.BytesIO()
    workbook.save(buffer)
    with zipfile.ZipFile(buffer, "a") as archive:
        archive.writestr("xl/vbaProject.bin", b"malicious")
    path = write_fixture(tmp_path, "macro.xlsx", buffer.getvalue())
    with pytest.raises(AppError) as error:
        validate_document(staged(path, "macro.xlsx"), settings)
    assert error.value.status_code == 415
    assert error.value.code == "macro_enabled_document"


def test_xlsm_extension_is_refused(settings: Settings) -> None:
    from app.documents.validation import supported_extension

    with pytest.raises(AppError) as error:
        supported_extension("book.xlsm")
    assert error.value.status_code == 415
