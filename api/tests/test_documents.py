from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import OutboxEvent
from app.infrastructure.scanner import MalwareDetectedError, ScannerUnavailableError
from tests.conftest import CleanScanner, InMemoryObjectStorage, SeededEntities
from tests.document_samples import (
    docx_bytes,
    encrypted_pdf_bytes,
    macro_default_docx_bytes,
    malformed_docx_bytes,
    pdf_bytes,
    xlsx_bytes,
)


@pytest.mark.parametrize(
    ("filename", "media_type", "content"),
    [
        ("report.pdf", "application/pdf", pdf_bytes()),
        (
            "memo.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            docx_bytes(),
        ),
        (
            "budget.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            xlsx_bytes(),
        ),
    ],
)
async def test_upload_supported_documents(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    object_storage: InMemoryObjectStorage,
    filename: str,
    media_type: str,
    content: bytes,
) -> None:
    response = await upload(
        client, auth_headers, entities.finance.id, filename, content, media_type
    )

    assert response.status_code == 201
    body = response.json()
    assert body["workspace_id"] == str(entities.finance.id)
    assert body["original_filename"] == filename
    assert body["detected_media_type"] == media_type
    assert body["status"] == "ready"
    assert body["duplicate"] is False
    assert len(object_storage.objects) == 1


async def test_upload_duplicate_returns_existing_document(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    object_storage: InMemoryObjectStorage,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    content = pdf_bytes()
    first = await upload(client, auth_headers, entities.finance.id, "first.pdf", content)
    second = await upload(client, auth_headers, entities.finance.id, "renamed.pdf", content)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["original_filename"] == "first.pdf"
    assert second.json()["duplicate"] is True
    assert len(object_storage.objects) == 1
    async with db_session_factory() as session:
        events = (await session.scalars(select(OutboxEvent))).all()
    assert len(events) == 1


async def test_upload_persists_document_ready_outbox_event(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await upload(
        client, auth_headers, entities.finance.id, "report.pdf", pdf_bytes()
    )

    assert response.status_code == 201
    document_id = response.json()["id"]
    async with db_session_factory() as session:
        event = await session.scalar(select(OutboxEvent))

    assert event is not None
    assert event.event_type == "document.ready"
    assert event.status == "pending"
    assert event.payload["id"] == str(event.id)
    assert event.payload["data"]["document_id"] == document_id
    assert event.payload["data"]["bucket"] == "raw-documents"
    assert event.payload["data"]["object_key"].startswith("workspaces/")


async def test_same_file_in_different_workspaces_is_stored_separately(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    object_storage: InMemoryObjectStorage,
) -> None:
    content = pdf_bytes()
    finance = await upload(client, auth_headers, entities.finance.id, "report.pdf", content)
    legal = await upload(client, auth_headers, entities.legal.id, "report.pdf", content)

    assert finance.status_code == 201
    assert legal.status_code == 201
    assert finance.json()["id"] != legal.json()["id"]
    assert len(object_storage.objects) == 2
    object_keys = {key for _, key in object_storage.objects}
    assert any(f"workspaces/{entities.finance.id}/" in key for key in object_keys)
    assert any(f"workspaces/{entities.legal.id}/" in key for key in object_keys)


async def test_document_cannot_be_read_through_another_workspace(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    content = pdf_bytes()
    created = await upload(client, auth_headers, entities.finance.id, "report.pdf", content)

    response = await client.get(
        f"/v1/workspaces/{entities.legal.id}/documents/{created.json()['id']}",
        headers=auth_headers,
    )

    assert response.status_code == 404
    assert response.json()["code"] == "document_not_found"


async def test_document_cannot_be_deleted_through_another_workspace(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    object_storage: InMemoryObjectStorage,
) -> None:
    created = await upload(client, auth_headers, entities.finance.id, "report.pdf", pdf_bytes())

    response = await client.delete(
        f"/v1/workspaces/{entities.legal.id}/documents/{created.json()['id']}",
        headers=auth_headers,
    )

    assert response.status_code == 404
    assert len(object_storage.objects) == 1


async def test_download_streams_original_bytes(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    content = pdf_bytes()
    created = await upload(client, auth_headers, entities.finance.id, "laporan 2026.pdf", content)

    response = await client.get(
        f"/v1/workspaces/{entities.finance.id}/documents/{created.json()['id']}/download",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["cache-control"] == "private, no-store"
    assert "filename*=UTF-8''laporan%202026.pdf" in response.headers["content-disposition"]


async def test_workspace_viewer_cannot_download_original_document(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    created = await upload(client, auth_headers, entities.legal.id, "report.pdf", pdf_bytes())
    viewer_login = await client.post(
        "/v1/auth/token",
        data={"username": "bob", "password": "another-correct-password"},
    )

    response = await client.get(
        f"/v1/workspaces/{entities.legal.id}/documents/{created.json()['id']}/download",
        headers={"Authorization": f"Bearer {viewer_login.json()['access_token']}"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "insufficient_workspace_permission"


async def test_delete_removes_metadata_and_object(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    object_storage: InMemoryObjectStorage,
) -> None:
    content = pdf_bytes()
    created = await upload(client, auth_headers, entities.finance.id, "report.pdf", content)
    url = f"/v1/workspaces/{entities.finance.id}/documents/{created.json()['id']}"

    deleted = await client.delete(url, headers=auth_headers)
    fetched = await client.get(url, headers=auth_headers)

    assert deleted.status_code == 204
    assert fetched.status_code == 404
    assert object_storage.objects == {}

    reuploaded = await upload(
        client, auth_headers, entities.finance.id, "report-again.pdf", content
    )
    assert reuploaded.status_code == 201
    assert reuploaded.json()["id"] != created.json()["id"]


async def test_delete_persists_document_deleted_outbox_event(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    created = await upload(client, auth_headers, entities.finance.id, "report.pdf", pdf_bytes())

    response = await client.delete(
        f"/v1/workspaces/{entities.finance.id}/documents/{created.json()['id']}",
        headers=auth_headers,
    )
    async with db_session_factory() as session:
        event = await session.scalar(
            select(OutboxEvent).where(OutboxEvent.event_type == "document.deleted")
        )

    assert response.status_code == 204
    assert event is not None
    assert event.aggregate_type == "document"
    assert str(event.aggregate_id) == created.json()["id"]
    assert event.workspace_id == entities.finance.id
    assert event.payload["id"] == str(event.id)
    assert event.payload["type"] == "document.deleted"
    assert event.payload["source"] == "enterprise-ai-ingestion"
    assert event.payload["subject"] == f"document/{created.json()['id']}"
    assert event.payload["schema_version"] == 1
    assert event.payload["occurred_at"]
    assert event.payload["data"] == {
        "document_id": created.json()["id"],
        "workspace_id": str(entities.finance.id),
        "sha256": created.json()["sha256"],
    }


async def test_rejects_extension_content_mismatch(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    response = await upload(
        client, auth_headers, entities.finance.id, "not-a-word-file.docx", pdf_bytes()
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_document"


async def test_rejects_encrypted_pdf(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    response = await upload(
        client, auth_headers, entities.finance.id, "secret.pdf", encrypted_pdf_bytes()
    )

    assert response.status_code == 422
    assert response.json()["code"] == "encrypted_document"


async def test_rejects_encrypted_pdf_with_empty_user_password(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    response = await upload(
        client,
        auth_headers,
        entities.finance.id,
        "owner-locked.pdf",
        encrypted_pdf_bytes(user_password=""),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "encrypted_document"


async def test_rejects_macro_declared_as_default_content_type(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    response = await upload(
        client,
        auth_headers,
        entities.finance.id,
        "macro.docx",
        macro_default_docx_bytes(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert response.status_code == 415
    assert response.json()["code"] == "macro_enabled_document"


async def test_rejects_malformed_office_main_xml(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    response = await upload(
        client,
        auth_headers,
        entities.finance.id,
        "broken.docx",
        malformed_docx_bytes(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_document"


async def test_rejects_oversized_file(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    response = await upload(
        client,
        auth_headers,
        entities.finance.id,
        "huge.pdf",
        b"%PDF-" + b"x" * (1024 * 1024),
    )

    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"


async def test_rejects_malware(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    scanner: CleanScanner,
) -> None:
    scanner.error = MalwareDetectedError()
    response = await upload(client, auth_headers, entities.finance.id, "infected.pdf", pdf_bytes())

    assert response.status_code == 422
    assert response.json()["code"] == "malware_detected"


async def test_scanner_outage_fails_closed(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    scanner: CleanScanner,
    object_storage: InMemoryObjectStorage,
) -> None:
    scanner.error = ScannerUnavailableError()
    response = await upload(client, auth_headers, entities.finance.id, "report.pdf", pdf_bytes())

    assert response.status_code == 503
    assert response.json()["code"] == "malware_scanner_unavailable"
    assert object_storage.objects == {}


async def test_storage_upload_failure_does_not_create_metadata(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    object_storage: InMemoryObjectStorage,
) -> None:
    object_storage.upload_error = True
    response = await upload(client, auth_headers, entities.finance.id, "report.pdf", pdf_bytes())
    listing = await client.get(
        f"/v1/workspaces/{entities.finance.id}/documents", headers=auth_headers
    )

    assert response.status_code == 503
    assert listing.json()["total"] == 0
    assert object_storage.objects == {}


async def test_missing_stored_object_returns_bad_gateway(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    object_storage: InMemoryObjectStorage,
) -> None:
    created = await upload(client, auth_headers, entities.finance.id, "report.pdf", pdf_bytes())
    object_storage.objects.clear()

    response = await client.get(
        f"/v1/workspaces/{entities.finance.id}/documents/{created.json()['id']}/download",
        headers=auth_headers,
    )

    assert response.status_code == 502
    assert response.json()["code"] == "stored_object_missing"


async def test_failed_delete_can_be_retried_from_deleting_state(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    object_storage: InMemoryObjectStorage,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    created = await upload(client, auth_headers, entities.finance.id, "report.pdf", pdf_bytes())
    url = f"/v1/workspaces/{entities.finance.id}/documents/{created.json()['id']}"
    object_storage.delete_error = True

    failed = await client.delete(url, headers=auth_headers)
    metadata = await client.get(url, headers=auth_headers)

    assert failed.status_code == 503
    assert metadata.status_code == 200
    assert metadata.json()["status"] == "deleting"
    assert len(object_storage.objects) == 1
    async with db_session_factory() as session:
        failed_events = (
            await session.scalars(
                select(OutboxEvent).where(OutboxEvent.event_type == "document.deleted")
            )
        ).all()
    assert len(failed_events) == 1
    failed_event_id = failed_events[0].id
    failed_event_payload = failed_events[0].payload
    assert failed_event_payload["id"] == str(failed_event_id)
    assert failed_event_payload["data"]["document_id"] == created.json()["id"]

    object_storage.delete_error = False
    retried = await client.delete(url, headers=auth_headers)
    assert retried.status_code == 204
    assert (await client.get(url, headers=auth_headers)).status_code == 404
    async with db_session_factory() as session:
        deleted_events = (
            await session.scalars(
                select(OutboxEvent).where(OutboxEvent.event_type == "document.deleted")
            )
        ).all()
    assert len(deleted_events) == 1
    assert deleted_events[0].id == failed_event_id
    assert deleted_events[0].payload == failed_event_payload


async def test_upload_requires_valid_jwt_before_multipart_is_parsed(
    client: httpx.AsyncClient,
    entities: SeededEntities,
    object_storage: InMemoryObjectStorage,
) -> None:
    response = await client.post(
        f"/v1/workspaces/{entities.finance.id}/documents",
        files={"file": ("report.pdf", pdf_bytes(), "application/pdf")},
    )

    assert response.status_code == 401
    assert object_storage.objects == {}


async def test_chunked_upload_limit_returns_413(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    object_storage: InMemoryObjectStorage,
) -> None:
    boundary = "upload-boundary"

    async def oversized_multipart() -> AsyncIterator[bytes]:
        yield (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="huge.pdf"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode()
        yield b"%PDF-" + b"x" * (3 * 1024 * 1024)
        yield f"\r\n--{boundary}--\r\n".encode()

    response = await client.post(
        f"/v1/workspaces/{entities.finance.id}/documents",
        headers={
            **auth_headers,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        content=oversized_multipart(),
    )

    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"
    assert object_storage.objects == {}


async def upload(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    workspace_id: object,
    filename: str,
    content: bytes,
    content_type: str = "application/pdf",
) -> httpx.Response:
    return await client.post(
        f"/v1/workspaces/{workspace_id}/documents",
        headers=headers,
        files={"file": (filename, content, content_type)},
    )
