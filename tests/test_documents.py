import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    JOB_DELETE,
    JOB_EXTRACT,
    RUN_FAILED,
    RUN_QUEUED,
    Document,
    DocumentChunk,
    DocumentWorkspace,
    ProcessingRun,
)
from app.documents.validation import StagedUpload
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
    pptx_bytes,
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
        (
            "deck.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            pptx_bytes(),
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

    assert response.status_code == 202
    body = response.json()
    assert body["workspace_id"] == str(entities.finance.id)
    assert body["original_filename"] == filename
    assert body["detected_media_type"] == media_type
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
    response = await upload(client, auth_headers, entities.finance.id, "report.pdf", pdf_bytes())

    assert response.status_code == 202
    document_id = UUID(response.json()["id"])
    async with db_session_factory() as session:
        run = await session.scalar(select(ProcessingRun))
        document = await session.get(Document, document_id)

    assert run is not None
    assert run.document_id == document_id
    assert run.origin_workspace_id == entities.finance.id
    assert run.job_kind == JOB_EXTRACT
    assert run.status == RUN_QUEUED
    assert document is not None
    assert document.active_processing_run_id == run.id
    assert document.bucket == "raw-documents"
    assert document.object_key.startswith("workspaces/")


async def test_the_same_file_in_two_workspaces_is_one_document_with_two_links(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    object_storage: InMemoryObjectStorage,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    content = pdf_bytes()
    finance = await upload(client, auth_headers, entities.finance.id, "report.pdf", content)
    legal = await upload(client, auth_headers, entities.legal.id, "laporan.pdf", content)

    assert finance.status_code == 202
    assert legal.status_code == 200
    assert finance.json()["id"] == legal.json()["id"]
    assert legal.json()["duplicate"] is True
    assert len(object_storage.objects) == 1
    async with db_session_factory() as session:
        links = (await session.scalars(select(DocumentWorkspace))).all()
        extract_runs = (
            await session.scalars(
                select(ProcessingRun).where(ProcessingRun.job_kind == JOB_EXTRACT)
            )
        ).all()
    assert {(link.workspace_id, link.filename) for link in links} == {
        (entities.finance.id, "report.pdf"),
        (entities.legal.id, "laporan.pdf"),
    }
    assert len(extract_runs) == 1


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


async def test_cited_chunk_regions_are_scoped_to_the_active_document_run(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from uuid import uuid4

    created = await upload(
        client,
        auth_headers,
        entities.finance.id,
        "report.pdf",
        pdf_bytes(),
    )
    document_id = UUID(created.json()["id"])
    async with db_session_factory() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        run = await session.scalar(
            select(ProcessingRun).where(
                ProcessingRun.document_id == document.id,
                ProcessingRun.job_kind == JOB_EXTRACT,
            )
        )
        assert run is not None
        document.active_processing_run_id = run.id
        session.add(
            DocumentChunk(
                id=uuid4(),
                origin_workspace_id=entities.finance.id,
                document_id=document.id,
                processing_run_id=run.id,
                ordinal=4,
                text="Cited prose.",
                token_count=2,
                content_hash=hashlib.sha256(b"Cited prose.").hexdigest(),
                section_path=["Results"],
                section_key="",
                is_heading_only=False,
                page_numbers=[2],
                provenance={
                    "regions": [
                        {
                            "page_no": 2,
                            "bbox": [0.1, 0.2, 0.5, 0.3],
                            "kind": "text",
                            "ref": "",
                        },
                        {
                            "kind": "docx-block",
                            "anchor": "docx-4",
                            "ref": "",
                        },
                        {
                            "page_no": 2,
                            "bbox": [0.2, 0.4, 0.6, 0.45],
                            "kind": "ocr",
                            "ref": "",
                            "text": "OCR line",
                        },
                    ]
                },
            )
        )
        await session.commit()

    response = await client.get(
        f"/v1/workspaces/{entities.finance.id}/documents/{document_id}/chunks/4/regions",
        headers=auth_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "regions": [
            {
                "page_no": 2,
                "bbox": [0.1, 0.2, 0.5, 0.3],
                "kind": "text",
                "ref": "",
                "anchor": "",
                "text": "",
            },
            {
                "page_no": None,
                "bbox": None,
                "kind": "docx-block",
                "ref": "",
                "anchor": "docx-4",
                "text": "",
            },
            {
                "page_no": 2,
                "bbox": [0.2, 0.4, 0.6, 0.45],
                "kind": "ocr",
                "ref": "",
                "anchor": "",
                "text": "OCR line",
            },
        ]
    }

    text_layer = await client.get(
        f"/v1/workspaces/{entities.finance.id}/documents/{document_id}/text-layer",
        headers=auth_headers,
    )
    assert text_layer.status_code == 200, text_layer.text
    assert text_layer.json() == {
        "pages": [
            {
                "page_no": 2,
                "lines": [
                    {
                        "text": "OCR line",
                        "bbox": [0.2, 0.4, 0.6, 0.45],
                    }
                ],
            }
        ]
    }


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


async def test_download_can_be_requested_inline_for_preview(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    """`inline` is what the preview needs: render, do not save."""
    content = pdf_bytes()
    created = await upload(client, auth_headers, entities.finance.id, "laporan 2026.pdf", content)
    document_id = created.json()["id"]

    response = await client.get(
        f"/v1/workspaces/{entities.finance.id}/documents/{document_id}/download",
        headers=auth_headers,
        params={"disposition": "inline"},
    )

    assert response.status_code == 200
    assert response.content == content
    assert response.headers["content-disposition"].startswith("inline;")
    assert "filename*=UTF-8''laporan%202026.pdf" in response.headers["content-disposition"]
    assert response.headers["cache-control"] == "private, max-age=300"
    assert response.headers["etag"] == f'"{created.json()["sha256"]}"'


async def test_inline_download_keeps_the_download_disposition_as_default(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    created = await upload(client, auth_headers, entities.finance.id, "report.pdf", pdf_bytes())

    response = await client.get(
        f"/v1/workspaces/{entities.finance.id}/documents/{created.json()['id']}/download",
        headers=auth_headers,
    )

    assert response.headers["content-disposition"].startswith("attachment;")
    assert "etag" not in response.headers


async def test_a_viewer_still_cannot_request_an_inline_download(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    """Inline is a disposition, not a permission: the role check is unchanged."""
    created = await upload(client, auth_headers, entities.legal.id, "report.pdf", pdf_bytes())
    viewer_login = await client.post(
        "/v1/auth/token",
        data={"username": "bob", "password": "another-correct-password"},
    )
    viewer_headers = {"Authorization": f"Bearer {viewer_login.json()['access_token']}"}

    response = await client.get(
        f"/v1/workspaces/{entities.legal.id}/documents/{created.json()['id']}/download",
        headers=viewer_headers,
        params={"disposition": "inline"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "insufficient_workspace_permission"


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
    """The URL a get_document_link tool hands out is fetched with the same key.

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
        await revoke_api_key(session, key_id=key.record.id, user_id=entities.user.id)
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


async def test_reindex_queues_a_fresh_extract_run(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The reindex creates a second extract run and moves the active pointer.

    The original run is marked terminal first, because a document whose
    upload is still queued refuses a reindex -- that is the conflict guard
    doing its job.
    """
    created = await upload(client, auth_headers, entities.finance.id, "report.pdf", pdf_bytes())
    document_id = UUID(created.json()["id"])
    url = f"/v1/workspaces/{entities.finance.id}/documents/{document_id}"
    async with db_session_factory() as session, session.begin():
        first_run = await session.scalar(
            select(ProcessingRun).where(ProcessingRun.document_id == document_id)
        )
        assert first_run is not None
        first_run.status = RUN_FAILED

    response = await client.post(f"{url}/reindex", headers=auth_headers)

    assert response.status_code == 202
    assert response.json()["status"] == "processing"
    async with db_session_factory() as session:
        runs = (
            await session.scalars(
                select(ProcessingRun).where(
                    ProcessingRun.document_id == document_id,
                    ProcessingRun.job_kind == JOB_EXTRACT,
                )
            )
        ).all()
        document = await session.get(Document, document_id)
    assert document is not None
    assert len(runs) == 2
    reindexed = next(run for run in runs if run.id != first_run.id)
    assert document.active_processing_run_id == reindexed.id


async def test_reindex_refuses_while_a_run_is_in_flight(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    """The upload's run is still queued, so the reindex must wait behind it."""
    created = await upload(client, auth_headers, entities.finance.id, "report.pdf", pdf_bytes())
    url = f"/v1/workspaces/{entities.finance.id}/documents/{created.json()['id']}"

    response = await client.post(f"{url}/reindex", headers=auth_headers)

    assert response.status_code == 409
    assert response.json()["code"] == "reindex_conflict"


async def test_reindex_is_owner_only(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    """A viewer of the workspace cannot re-run a whole pipeline."""
    created = await upload(client, auth_headers, entities.legal.id, "report.pdf", pdf_bytes())
    url = f"/v1/workspaces/{entities.legal.id}/documents/{created.json()['id']}/reindex"
    login = await client.post(
        "/v1/auth/token",
        data={"username": "bob", "password": "another-correct-password"},
    )
    viewer_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = await client.post(url, headers=viewer_headers)

    assert response.status_code == 403


async def test_reindex_refuses_an_unknown_document(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    from uuid import uuid4

    response = await client.post(
        f"/v1/workspaces/{entities.finance.id}/documents/{uuid4()}/reindex",
        headers=auth_headers,
    )

    assert response.status_code == 404


async def test_reindex_refuses_a_workspace_whose_link_is_deleted(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A deleted link cannot re-run the canonical document for other workspaces."""
    content = pdf_bytes()
    finance = await upload(client, auth_headers, entities.finance.id, "report.pdf", content)
    await upload(client, auth_headers, entities.legal.id, "laporan.pdf", content)
    document_id = UUID(finance.json()["id"])
    async with db_session_factory() as session, session.begin():
        run = await session.scalar(
            select(ProcessingRun).where(ProcessingRun.document_id == document_id)
        )
        assert run is not None
        run.status = RUN_FAILED

    deleted = await client.delete(
        f"/v1/workspaces/{entities.finance.id}/documents/{document_id}",
        headers=auth_headers,
    )
    assert deleted.status_code == 204

    refused = await client.post(
        f"/v1/workspaces/{entities.finance.id}/documents/{document_id}/reindex",
        headers=auth_headers,
    )
    assert refused.status_code == 404

    # The live link in the other workspace is still allowed to reindex it.
    allowed = await client.post(
        f"/v1/workspaces/{entities.legal.id}/documents/{document_id}/reindex",
        headers=auth_headers,
    )
    assert allowed.status_code == 202


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
    fetched = await client.get(url, headers=auth_headers)
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "deleting"

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
    created = await upload(client, auth_headers, entities.finance.id, "report.pdf", pdf_bytes())
    url = f"/v1/workspaces/{entities.finance.id}/documents/{created.json()['id']}"

    first = await client.delete(url, headers=auth_headers)
    second = await client.delete(url, headers=auth_headers)

    assert first.status_code == second.status_code == 204
    async with db_session_factory() as session:
        runs = (
            await session.scalars(select(ProcessingRun).where(ProcessingRun.job_kind == JOB_DELETE))
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

    assert failed.status_code == 503
    assert metadata.status_code == 200
    assert metadata.json()["status"] == "deleting"
    assert len(object_storage.objects) == 1
    async with db_session_factory() as session:
        delete_runs = (
            await session.scalars(select(ProcessingRun).where(ProcessingRun.job_kind == JOB_DELETE))
        ).all()
    assert len(delete_runs) == 1

    object_storage.delete_error = False
    retried = await client.delete(url, headers=auth_headers)
    assert retried.status_code == 204
    async with db_session_factory() as session:
        retried_runs = (
            await session.scalars(select(ProcessingRun).where(ProcessingRun.job_kind == JOB_DELETE))
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


async def test_document_list_reports_derived_status_and_paginates(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    for name, text in (("first.pdf", "First"), ("second.pdf", "Second")):
        created = await upload(client, auth_headers, entities.finance.id, name, pdf_bytes(text))
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
    created = await upload(client, auth_headers, entities.finance.id, "report.pdf", pdf_bytes())
    document_id = created.json()["id"]
    url = f"/v1/workspaces/{entities.finance.id}/documents/{document_id}"

    before = await client.get(
        f"/v1/workspaces/{entities.finance.id}/documents", headers=auth_headers
    )
    assert document_id in {item["id"] for item in before.json()["items"]}

    assert (await client.delete(url, headers=auth_headers)).status_code == 204

    async with db_session_factory() as session:
        stored = await session.get(Document, UUID(document_id))
    assert stored is not None and stored.deleted_at is not None

    after = await client.get(
        f"/v1/workspaces/{entities.finance.id}/documents", headers=auth_headers
    )
    assert document_id not in {item["id"] for item in after.json()["items"]}
    assert after.json()["total"] == 0

    fetched = await client.get(url, headers=auth_headers)
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "deleting"


async def test_document_list_is_scoped_to_one_workspace(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    await upload(client, auth_headers, entities.finance.id, "report.pdf", pdf_bytes())

    other = await client.get(f"/v1/workspaces/{entities.legal.id}/documents", headers=auth_headers)

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
    owner_headers = {"Authorization": f"Bearer {owner_login.json()['access_token']}"}
    viewer_login = await client.post(
        "/v1/auth/token",
        data={"username": "bob", "password": "another-correct-password"},
    )
    viewer_headers = {"Authorization": f"Bearer {viewer_login.json()['access_token']}"}

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


EXPECTED_INDEXABLE = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/csv",
}


async def test_deleting_one_link_keeps_the_document_for_the_other_workspace(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    object_storage: InMemoryObjectStorage,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    content = pdf_bytes()
    finance = await upload(client, auth_headers, entities.finance.id, "report.pdf", content)
    await upload(client, auth_headers, entities.legal.id, "laporan.pdf", content)
    document_id = UUID(finance.json()["id"])

    deleted = await client.delete(
        f"/v1/workspaces/{entities.finance.id}/documents/{document_id}",
        headers=auth_headers,
    )

    assert deleted.status_code == 204
    assert len(object_storage.objects) == 1
    async with db_session_factory() as session:
        document = await session.get(Document, document_id)
        finance_link = await session.scalar(
            select(DocumentWorkspace).where(
                DocumentWorkspace.workspace_id == entities.finance.id,
                DocumentWorkspace.document_id == document_id,
            )
        )
        legal_link = await session.scalar(
            select(DocumentWorkspace).where(
                DocumentWorkspace.workspace_id == entities.legal.id,
                DocumentWorkspace.document_id == document_id,
            )
        )
        delete_runs = (
            await session.scalars(select(ProcessingRun).where(ProcessingRun.job_kind == JOB_DELETE))
        ).all()
    assert document is not None and document.deleted_at is None
    assert finance_link is not None and finance_link.deleted_at is not None
    assert legal_link is not None and legal_link.deleted_at is None
    assert delete_runs == []

    metadata = await client.get(
        f"/v1/workspaces/{entities.legal.id}/documents/{document_id}",
        headers=auth_headers,
    )
    assert metadata.status_code == 200
    assert metadata.json()["original_filename"] == "laporan.pdf"
    assert metadata.json()["status"] == "processing"


async def test_deleting_the_last_link_queues_cleanup_and_removes_the_object(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    object_storage: InMemoryObjectStorage,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    content = pdf_bytes()
    finance = await upload(client, auth_headers, entities.finance.id, "report.pdf", content)
    await upload(client, auth_headers, entities.legal.id, "laporan.pdf", content)
    document_id = UUID(finance.json()["id"])

    first = await client.delete(
        f"/v1/workspaces/{entities.finance.id}/documents/{document_id}",
        headers=auth_headers,
    )
    second = await client.delete(
        f"/v1/workspaces/{entities.legal.id}/documents/{document_id}",
        headers=auth_headers,
    )

    assert first.status_code == second.status_code == 204
    assert object_storage.objects == {}
    async with db_session_factory() as session:
        document = await session.get(Document, document_id)
        delete_runs = (
            await session.scalars(select(ProcessingRun).where(ProcessingRun.job_kind == JOB_DELETE))
        ).all()
    assert document is not None and document.deleted_at is not None
    assert len(delete_runs) == 1


async def test_reuploading_to_a_workspace_reactivates_its_deleted_link(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    object_storage: InMemoryObjectStorage,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    content = pdf_bytes()
    first = await upload(client, auth_headers, entities.finance.id, "report.pdf", content)
    await upload(client, auth_headers, entities.legal.id, "laporan.pdf", content)
    document_id = UUID(first.json()["id"])

    await client.delete(
        f"/v1/workspaces/{entities.finance.id}/documents/{document_id}",
        headers=auth_headers,
    )
    again = await upload(client, auth_headers, entities.finance.id, "report-again.pdf", content)

    assert again.status_code == 200
    assert again.json()["duplicate"] is True
    assert len(object_storage.objects) == 1
    async with db_session_factory() as session:
        link = await session.scalar(
            select(DocumentWorkspace).where(
                DocumentWorkspace.workspace_id == entities.finance.id,
                DocumentWorkspace.document_id == document_id,
            )
        )
    assert link is not None
    assert link.deleted_at is None
    assert link.filename == "report-again.pdf"


def test_a_deck_is_validated_like_the_other_office_packages(tmp_path: Path) -> None:
    """The OOXML checks apply to a deck by naming its own main part."""
    from app.core.config import get_settings
    from app.documents.validation import PPTX_MEDIA_TYPE, validate_document

    path = tmp_path / "deck.pptx"
    path.write_bytes(pptx_bytes())
    staged = StagedUpload(
        path=path,
        original_filename="deck.pptx",
        extension=".pptx",
        size_bytes=path.stat().st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )

    result = validate_document(staged, get_settings())

    assert result.media_type == PPTX_MEDIA_TYPE


def test_a_renamed_zip_without_presentation_parts_is_refused(tmp_path: Path) -> None:
    """An .pptx extension on something that is not a deck must not pass."""
    from app.core.config import get_settings
    from app.core.errors import AppError
    from app.documents.validation import validate_document

    path = tmp_path / "fake.pptx"
    path.write_bytes(docx_bytes())  # a real OOXML package, wrong kind
    staged = StagedUpload(
        path=path,
        original_filename="fake.pptx",
        extension=".pptx",
        size_bytes=path.stat().st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )

    with pytest.raises(AppError) as caught:
        validate_document(staged, get_settings())
    assert caught.value.status_code == 422


def test_a_deck_over_the_slide_cap_is_refused_before_conversion(tmp_path: Path) -> None:
    """The deck is measured by its own slide table, not by rendering it first.

    Converting first and refusing after would spend a minute of LibreOffice on
    a file that was never going to be accepted.
    """
    from app.core.config import get_settings
    from app.core.errors import AppError
    from app.documents.validation import validate_document

    settings = get_settings().model_copy(update={"max_pdf_pages": 2})
    path = tmp_path / "long-deck.pptx"
    path.write_bytes(pptx_bytes(slides=3))
    staged = StagedUpload(
        path=path,
        original_filename="long-deck.pptx",
        extension=".pptx",
        size_bytes=path.stat().st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )

    with pytest.raises(AppError) as caught:
        validate_document(staged, settings)

    assert caught.value.code == "too_many_pages"
    assert "3 slides" not in str(caught.value.detail)
    assert "Presentations cannot contain more than 2 slides." in caught.value.detail


def test_a_deck_at_the_slide_cap_is_accepted(tmp_path: Path) -> None:
    from app.core.config import get_settings
    from app.documents.validation import validate_document

    settings = get_settings().model_copy(update={"max_pdf_pages": 3})
    path = tmp_path / "deck-at-cap.pptx"
    path.write_bytes(pptx_bytes(slides=3))
    staged = StagedUpload(
        path=path,
        original_filename="deck-at-cap.pptx",
        extension=".pptx",
        size_bytes=path.stat().st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )

    result = validate_document(staged, settings)

    assert result.media_type.endswith("presentation")


def test_the_page_cap_default_is_a_thousand() -> None:
    from app.core.config import Settings

    assert Settings.model_fields["max_pdf_pages"].default == 1_000
