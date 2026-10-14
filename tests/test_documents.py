from collections.abc import AsyncIterator
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    JOB_DELETE,
    JOB_EXTRACT,
    RUN_QUEUED,
    Document,
    ProcessingRun,
)
from app.infrastructure.scanner import MalwareDetectedError, ScannerUnavailableError
from app.security.api_keys import mint_api_key, revoke_api_key
from tests.conftest import CleanScanner, InMemoryObjectStorage, SeededEntities
from tests.document_samples import (
    csv_bytes,
    docx_bytes,
    encrypted_pdf_bytes,
    macro_default_docx_bytes,
    malformed_docx_bytes,
    pdf_bytes,
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
        ("sales.csv", "text/csv", csv_bytes()),
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

    assert response.status_code == 202
    body = response.json()
    assert body["workspace_id"] == str(entities.finance.id)
    assert body["original_filename"] == filename
    assert body["detected_media_type"] == media_type
    # Derived from the enqueued run, not stored on the document. A
    # spreadsheet is accepted and stored but the extraction pipeline
    # cannot read it, so the run is already terminal when the response is
    # built. Previously the document reported "ready" and the failure only
    # surfaced in the processing feed; the two could disagree.
    expected = "processing" if media_type in EXPECTED_INDEXABLE else "unsupported_format"
    assert body["status"] == expected
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

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["original_filename"] == "first.pdf"
    assert second.json()["duplicate"] is True
    assert len(object_storage.objects) == 1
    # A duplicate is the same document, so it must not queue a second run.
    async with db_session_factory() as session:
        runs = (await session.scalars(select(ProcessingRun))).all()
    assert len(runs) == 1


async def test_upload_enqueues_the_pipeline_in_the_same_transaction(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The document and its run commit together.

    There is no outbox and no webhook: a document that exists is a
    document the worker will pick up.
    """
    response = await upload(
        client, auth_headers, entities.finance.id, "report.pdf", pdf_bytes()
    )

    assert response.status_code == 202
    document_id = UUID(response.json()["id"])
    async with db_session_factory() as session:
        run = await session.scalar(select(ProcessingRun))
        document = await session.get(Document, document_id)

    assert run is not None
    assert run.document_id == document_id
    assert run.workspace_id == entities.finance.id
    assert run.job_kind == JOB_EXTRACT
    assert run.status == RUN_QUEUED
    assert document is not None
    assert document.active_processing_run_id == run.id
    assert document.bucket == "raw-documents"
    assert document.object_key.startswith("workspaces/")


async def test_same_file_in_different_workspaces_is_stored_separately(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    object_storage: InMemoryObjectStorage,
) -> None:
    content = pdf_bytes()
    finance = await upload(client, auth_headers, entities.finance.id, "report.pdf", content)
    legal = await upload(client, auth_headers, entities.legal.id, "report.pdf", content)

    assert finance.status_code == 202
    assert legal.status_code == 202
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


async def test_download_accepts_an_api_key(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The URL a download_document tool hands out is fetched with the same key.

    This is the one route where an MCP credential is honoured, and it exists
    so the agent can retrieve the bytes the tool pointed it at without the
    file passing through the model.
    """
    content = pdf_bytes()
    created = await upload(client, auth_headers, entities.finance.id, "report.pdf", content)
    async with db_session_factory() as session:
        key = await mint_api_key(session, user_id=entities.user.id, name="agent")
        await session.commit()

    response = await client.get(
        f"/v1/workspaces/{entities.finance.id}/documents/{created.json()['id']}/download",
        headers={"Authorization": f"Bearer {key.plaintext}"},
    )

    assert response.status_code == 200
    assert response.content == content


async def test_download_rejects_a_revoked_api_key(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    created = await upload(client, auth_headers, entities.finance.id, "report.pdf", pdf_bytes())
    async with db_session_factory() as session:
        key = await mint_api_key(session, user_id=entities.user.id, name="agent")
        await revoke_api_key(
            session, key_id=key.record.id, user_id=entities.user.id
        )
        await session.commit()

    response = await client.get(
        f"/v1/workspaces/{entities.finance.id}/documents/{created.json()['id']}/download",
        headers={"Authorization": f"Bearer {key.plaintext}"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"


async def test_an_api_key_reaches_nothing_but_the_download(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Widening one route must not widen the others.

    The write endpoints stay JWT-only, so an agent key cannot upload or
    delete even inside a workspace the same key can read from.
    """
    created = await upload(client, auth_headers, entities.finance.id, "report.pdf", pdf_bytes())
    async with db_session_factory() as session:
        key = await mint_api_key(session, user_id=entities.user.id, name="agent")
        await session.commit()
    headers = {"Authorization": f"Bearer {key.plaintext}"}

    delete = await client.delete(
        f"/v1/workspaces/{entities.finance.id}/documents/{created.json()['id']}",
        headers=headers,
    )
    upload_response = await client.post(
        f"/v1/workspaces/{entities.finance.id}/documents",
        headers=headers,
        files={"file": ("report.pdf", pdf_bytes(), "application/pdf")},
    )

    assert delete.status_code == 401
    assert upload_response.status_code == 401


async def test_download_without_any_credential_is_refused(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    created = await upload(client, auth_headers, entities.finance.id, "report.pdf", pdf_bytes())

    response = await client.get(
        f"/v1/workspaces/{entities.finance.id}/documents/{created.json()['id']}/download"
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_delete_marks_the_document_and_queues_the_cleanup(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    object_storage: InMemoryObjectStorage,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The row survives the request.

    The worker needs the embedding ledger to find the Qdrant points, so
    the document is only removed once that cleanup has happened. The
    raw object goes immediately because nothing downstream needs it.
    """
    content = pdf_bytes()
    created = await upload(client, auth_headers, entities.finance.id, "report.pdf", content)
    document_id = UUID(created.json()["id"])
    url = f"/v1/workspaces/{entities.finance.id}/documents/{document_id}"

    deleted = await client.delete(url, headers=auth_headers)

    assert deleted.status_code == 204
    # The raw object is gone right away.
    assert object_storage.objects == {}

    async with db_session_factory() as session:
        document = await session.get(Document, document_id)
        run = await session.scalar(
            select(ProcessingRun).where(ProcessingRun.job_kind == JOB_DELETE)
        )
    assert document is not None, "the document row must survive until the worker runs"
    assert document.deleted_at is not None
    assert run is not None
    assert run.document_id == document_id
    assert run.status == RUN_QUEUED
    # The metadata route still resolves it, and reports it as deleting.
    fetched = await client.get(url, headers=auth_headers)
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "deleting"

    # The row still holds the (workspace_id, sha256) key until the worker
    # removes it, so identical bytes are refused while the cleanup is in
    # flight. Deleting the row here instead would orphan the vectors: the
    # Qdrant point IDs live in the rows that cascade away with it.
    reuploaded = await upload(
        client, auth_headers, entities.finance.id, "report-again.pdf", content
    )
    assert reuploaded.status_code == 409
    assert reuploaded.json()["code"] == "document_deletion_in_progress"


async def test_repeated_delete_does_not_queue_a_second_cleanup_run(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    created = await upload(
        client, auth_headers, entities.finance.id, "report.pdf", pdf_bytes()
    )
    url = f"/v1/workspaces/{entities.finance.id}/documents/{created.json()['id']}"

    first = await client.delete(url, headers=auth_headers)
    second = await client.delete(url, headers=auth_headers)

    assert first.status_code == second.status_code == 204
    async with db_session_factory() as session:
        runs = (
            await session.scalars(
                select(ProcessingRun).where(ProcessingRun.job_kind == JOB_DELETE)
            )
        ).all()
    assert len(runs) == 1, "a second delete must not queue a second cleanup"


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

    # The object could not be removed, so the request fails, but the
    # document is already marked and its cleanup is already queued. The
    # state is consistent: the API does not pretend the delete did not
    # start.
    assert failed.status_code == 503
    assert metadata.status_code == 200
    assert metadata.json()["status"] == "deleting"
    assert len(object_storage.objects) == 1
    async with db_session_factory() as session:
        delete_runs = (
            await session.scalars(
                select(ProcessingRun).where(ProcessingRun.job_kind == JOB_DELETE)
            )
        ).all()
    assert len(delete_runs) == 1

    object_storage.delete_error = False
    retried = await client.delete(url, headers=auth_headers)
    assert retried.status_code == 204
    async with db_session_factory() as session:
        retried_runs = (
            await session.scalars(
                select(ProcessingRun).where(ProcessingRun.job_kind == JOB_DELETE)
            )
        ).all()
    assert len(retried_runs) == 1, "the retry must reuse the queued run"


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


# --- listing ------------------------------------------------------------
#
# GET /v1/documents had no coverage at all, so the list the dashboard
# renders was unverified. These pin the parts the delete flow depends on.


async def test_document_list_reports_derived_status_and_paginates(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    for name, text in (("first.pdf", "First"), ("second.pdf", "Second")):
        created = await upload(
            client, auth_headers, entities.finance.id, name, pdf_bytes(text)
        )
        assert created.status_code == 202

    listed = await client.get(
        f"/v1/workspaces/{entities.finance.id}/documents", headers=auth_headers
    )

    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    assert {item["original_filename"] for item in body["items"]} == {
        "first.pdf",
        "second.pdf",
    }
    # Status is derived from the queued run, not stored on the row, so a
    # freshly accepted upload reports as still processing.
    assert {item["status"] for item in body["items"]} == {"processing"}

    page = await client.get(
        f"/v1/workspaces/{entities.finance.id}/documents?offset=1&limit=1",
        headers=auth_headers,
    )
    assert page.status_code == 200
    assert page.json()["total"] == 2, "total must describe the whole set, not the page"
    assert len(page.json()["items"]) == 1


async def test_document_list_excludes_a_document_being_deleted(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The row outlives the delete call on purpose, so it has to be
    filtered out of the list explicitly.

    Without the filter the document reappears on the next refresh --
    undownloadable and already unsearchable -- and stays there for as
    long as the worker is not running to remove it.
    """
    created = await upload(
        client, auth_headers, entities.finance.id, "report.pdf", pdf_bytes()
    )
    document_id = created.json()["id"]
    url = f"/v1/workspaces/{entities.finance.id}/documents/{document_id}"

    before = await client.get(
        f"/v1/workspaces/{entities.finance.id}/documents", headers=auth_headers
    )
    assert document_id in {item["id"] for item in before.json()["items"]}

    assert (await client.delete(url, headers=auth_headers)).status_code == 204

    # The row is still there for the worker to act on...
    async with db_session_factory() as session:
        stored = await session.get(Document, UUID(document_id))
    assert stored is not None and stored.deleted_at is not None

    # ...but the list must not report it as part of the workspace.
    after = await client.get(
        f"/v1/workspaces/{entities.finance.id}/documents", headers=auth_headers
    )
    assert document_id not in {item["id"] for item in after.json()["items"]}
    assert after.json()["total"] == 0

    # The single-document route still explains what happened.
    fetched = await client.get(url, headers=auth_headers)
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "deleting"


async def test_document_list_is_scoped_to_one_workspace(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    await upload(client, auth_headers, entities.finance.id, "report.pdf", pdf_bytes())

    other = await client.get(
        f"/v1/workspaces/{entities.legal.id}/documents", headers=auth_headers
    )

    assert other.status_code == 200
    assert other.json()["total"] == 0
    assert other.json()["items"] == []


async def test_viewer_can_list_documents_but_cannot_delete_one(
    client: httpx.AsyncClient,
    entities: SeededEntities,
) -> None:
    """can_delete is false for a viewer, and the API agrees with it.

    A client that ignored the capability and rendered the button anyway
    would get this 403 rather than a partial success.
    """
    owner_login = await client.post(
        "/v1/auth/token",
        data={"username": "alice", "password": "correct-horse-battery-staple"},
    )
    owner_headers = {
        "Authorization": f"Bearer {owner_login.json()['access_token']}"
    }
    viewer_login = await client.post(
        "/v1/auth/token",
        data={"username": "bob", "password": "another-correct-password"},
    )
    viewer_headers = {
        "Authorization": f"Bearer {viewer_login.json()['access_token']}"
    }

    created = await upload(
        client, owner_headers, entities.legal.id, "legal.pdf", pdf_bytes("Legal")
    )
    document_id = created.json()["id"]
    url = f"/v1/workspaces/{entities.legal.id}/documents/{document_id}"

    listed = await client.get(
        f"/v1/workspaces/{entities.legal.id}/documents", headers=viewer_headers
    )
    assert listed.status_code == 200, "a viewer has read access and may list"
    assert document_id in {item["id"] for item in listed.json()["items"]}

    refused = await client.delete(url, headers=viewer_headers)
    assert refused.status_code == 403
    assert refused.json()["code"] == "insufficient_workspace_permission"


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


# What the extraction pipeline can actually read, so a run starts queued
# rather than terminal. CSV is included: it is read as rows, not indexed as
# chunks, but the worker does parse it.
EXPECTED_INDEXABLE = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/csv",
}
