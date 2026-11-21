"""HTTP API tests for document intelligence profiles and manual-confirmed versioning."""

from __future__ import annotations

import hashlib
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    JOB_EXTRACT,
    PROFILE_STATUS_ENRICHED,
    Document,
    DocumentChunk,
    DocumentProfile,
    ProcessingRun,
)
from app.documents.intelligence import build_search_text
from app.documents.versions import detect_version_suggestions_for_document
from tests.conftest import SeededEntities
from tests.document_samples import pdf_bytes


async def _login(client: httpx.AsyncClient, username: str, password: str) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/token",
        data={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _upload_pdf(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    workspace_id: UUID,
    filename: str,
    text: str,
) -> UUID:
    response = await client.post(
        f"/v1/workspaces/{workspace_id}/documents",
        headers=headers,
        files={"file": (filename, pdf_bytes(text), "application/pdf")},
    )
    assert response.status_code == 202, response.text
    return UUID(response.json()["id"])


async def _seed_profile(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    document_id: UUID,
    title: str,
    entity_name: str,
    period_label: str,
    doc_category: str = "financial_report",
    doc_subtype: str = "10-K",
) -> None:
    entities_payload = [
        {
            "name": entity_name,
            "normalized": entity_name.lower().replace(" inc.", "").strip(),
            "type": "company",
            "aliases": [],
        }
    ]
    async with session_factory() as session, session.begin():
        session.add(
            DocumentProfile(
                document_id=document_id,
                status=PROFILE_STATUS_ENRICHED,
                title=title,
                doc_category=doc_category,
                doc_subtype=doc_subtype,
                primary_entities=entities_payload,
                counterparties=[],
                period_label=period_label,
                summary=f"{title} summary.",
                topics=["financials"],
                search_text=build_search_text(
                    title=title,
                    doc_category=doc_category,
                    doc_subtype=doc_subtype,
                    primary_entities=entities_payload,
                    counterparties=[],
                    period_label=period_label,
                    effective_date=None,
                    document_number=None,
                    version_label=None,
                    summary=f"{title} summary.",
                    topics=["financials"],
                ),
            )
        )


async def test_version_suggestions_confirm_dismiss_patch_and_list_enrichment(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ws_id = entities.finance.id
    doc_2024 = await _upload_pdf(client, auth_headers, ws_id, "10-K-2024.pdf", "Apple 2024")
    doc_2025 = await _upload_pdf(client, auth_headers, ws_id, "10-K-2025.pdf", "Apple 2025")
    await _seed_profile(
        db_session_factory,
        document_id=doc_2024,
        title="Apple Inc. Form 10-K FY2024",
        entity_name="Apple Inc.",
        period_label="FY2024",
    )
    await _seed_profile(
        db_session_factory,
        document_id=doc_2025,
        title="Apple Inc. Form 10-K FY2025",
        entity_name="Apple Inc.",
        period_label="FY2025",
    )
    async with db_session_factory() as session:
        await detect_version_suggestions_for_document(
            session, document_id=doc_2025, workspace_id=ws_id, commit=True
        )

    # Suggestions route is not shadowed by /{document_id}
    sug_resp = await client.get(
        f"/v1/workspaces/{ws_id}/documents/version-suggestions",
        headers=auth_headers,
    )
    assert sug_resp.status_code == 200, sug_resp.text
    suggestions = sug_resp.json()
    assert suggestions["total"] == 1
    suggestion_id = suggestions["items"][0]["id"]
    assert suggestions["items"][0]["suggested_newer_document_id"] == str(doc_2025)

    # Document list exposes profile + pending_version_suggestions before confirmation
    list_resp = await client.get(f"/v1/workspaces/{ws_id}/documents", headers=auth_headers)
    assert list_resp.status_code == 200
    by_id = {item["id"]: item for item in list_resp.json()["items"]}
    assert by_id[str(doc_2025)]["profile"]["title"] == "Apple Inc. Form 10-K FY2025"
    assert by_id[str(doc_2025)]["pending_version_suggestions"] == 1
    assert by_id[str(doc_2025)]["version"] is None

    # Confirm suggestion as supersedes
    confirm_resp = await client.post(
        f"/v1/workspaces/{ws_id}/documents/version-suggestions/{suggestion_id}/confirm",
        headers=auth_headers,
        json={"relation_kind": "supersedes", "newer_document_id": str(doc_2025)},
    )
    assert confirm_resp.status_code == 200, confirm_resp.text
    group = confirm_resp.json()
    group_id = group["group_id"]
    assert group["relation_kind"] == "supersedes"
    assert [m["document_id"] for m in group["members"]] == [str(doc_2024), str(doc_2025)]
    assert group["members"][-1]["is_latest"] is True

    # Document metadata & search now carry confirmed version signals
    meta_resp = await client.get(
        f"/v1/workspaces/{ws_id}/documents/{doc_2024}",
        headers=auth_headers,
    )
    assert meta_resp.status_code == 200
    meta = meta_resp.json()
    assert meta["version"]["is_superseded"] is True
    assert meta["version"]["latest_document_id"] == str(doc_2025)
    assert meta["pending_version_suggestions"] == 0

    search_resp = await client.get(
        "/v1/documents/search",
        headers=auth_headers,
        params={"q": "Apple 2024"},
    )
    assert search_resp.status_code == 200
    hits = search_resp.json()["items"]
    assert len(hits) == 1
    assert hits[0]["document_id"] == str(doc_2024)
    assert hits[0]["primary_entities"] == ["Apple Inc."]
    assert hits[0]["version"]["is_superseded"] is True

    # Patch group relation kind to periodic_series and rename
    patch_resp = await client.patch(
        f"/v1/workspaces/{ws_id}/documents/version-groups/{group_id}",
        headers=auth_headers,
        json={"name": "Apple Annual Reports", "relation_kind": "periodic_series"},
    )
    assert patch_resp.status_code == 200, patch_resp.text
    patched = patch_resp.json()
    assert patched["name"] == "Apple Annual Reports"
    assert patched["relation_kind"] == "periodic_series"

    # Detail endpoint returns profile, group, and empty pending_suggestions
    detail_resp = await client.get(
        f"/v1/workspaces/{ws_id}/documents/{doc_2025}/versions",
        headers=auth_headers,
    )
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["group"]["name"] == "Apple Annual Reports"
    assert detail["profile"]["doc_subtype"] == "10-K"
    assert detail["pending_suggestions"] == []

    # Unlink removes the group since only 1 doc remains
    unlink_resp = await client.delete(
        f"/v1/workspaces/{ws_id}/documents/{doc_2025}/versions",
        headers=auth_headers,
    )
    assert unlink_resp.status_code == 204
    after_unlink = await client.get(
        f"/v1/workspaces/{ws_id}/documents/{doc_2024}/versions",
        headers=auth_headers,
    )
    assert after_unlink.status_code == 200
    assert after_unlink.json()["group"] is None


async def test_viewer_cannot_mutate_versions_or_enrich(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Bob is a viewer in Legal: he can read suggestions/versions, but gets 403 on mutations."""
    ws_id = entities.legal.id
    doc_a = await _upload_pdf(client, auth_headers, ws_id, "Policy-v1.pdf", "Policy v1")
    doc_b = await _upload_pdf(client, auth_headers, ws_id, "Policy-v2.pdf", "Policy v2")
    await _seed_profile(
        db_session_factory,
        document_id=doc_a,
        title="Leave Policy v1",
        entity_name="Acme Corp",
        period_label="2024",
        doc_category="policy_sop",
        doc_subtype="HR Policy",
    )
    await _seed_profile(
        db_session_factory,
        document_id=doc_b,
        title="Leave Policy v2",
        entity_name="Acme Corp",
        period_label="2025",
        doc_category="policy_sop",
        doc_subtype="HR Policy",
    )
    async with db_session_factory() as session:
        suggestions = await detect_version_suggestions_for_document(
            session, document_id=doc_b, workspace_id=ws_id, commit=True
        )
    assert len(suggestions) == 1
    suggestion_id = suggestions[0].id

    viewer_headers = await _login(client, "bob", "another-correct-password")

    # Read endpoints succeed for viewer
    assert (
        await client.get(
            f"/v1/workspaces/{ws_id}/documents/version-suggestions",
            headers=viewer_headers,
        )
    ).status_code == 200
    assert (
        await client.get(
            f"/v1/workspaces/{ws_id}/documents/{doc_b}/versions",
            headers=viewer_headers,
        )
    ).status_code == 200

    # Mutation endpoints return 403 for viewer
    assert (
        await client.post(
            f"/v1/workspaces/{ws_id}/documents/version-suggestions/{suggestion_id}/confirm",
            headers=viewer_headers,
            json={},
        )
    ).status_code == 403
    assert (
        await client.post(
            f"/v1/workspaces/{ws_id}/documents/version-suggestions/{suggestion_id}/dismiss",
            headers=viewer_headers,
        )
    ).status_code == 403
    assert (
        await client.post(
            f"/v1/workspaces/{ws_id}/documents/{doc_b}/versions/link",
            headers=viewer_headers,
            json={"related_document_id": str(doc_a), "direction": "newer"},
        )
    ).status_code == 403
    assert (
        await client.delete(
            f"/v1/workspaces/{ws_id}/documents/{doc_b}/versions",
            headers=viewer_headers,
        )
    ).status_code == 403
    assert (
        await client.post(
            f"/v1/workspaces/{ws_id}/documents/{doc_b}/enrich",
            headers=viewer_headers,
        )
    ).status_code == 403

    # Owner can dismiss and manually link
    dismiss_resp = await client.post(
        f"/v1/workspaces/{ws_id}/documents/version-suggestions/{suggestion_id}/dismiss",
        headers=auth_headers,
    )
    assert dismiss_resp.status_code == 204

    link_resp = await client.post(
        f"/v1/workspaces/{ws_id}/documents/{doc_b}/versions/link",
        headers=auth_headers,
        json={
            "related_document_id": str(doc_a),
            "direction": "newer",
            "relation_kind": "supersedes",
            "version_note": "2025 revision",
        },
    )
    assert link_resp.status_code == 200, link_resp.text
    group_id = link_resp.json()["group"]["group_id"]

    # Viewer cannot PATCH group either
    assert (
        await client.patch(
            f"/v1/workspaces/{ws_id}/documents/version-groups/{group_id}",
            headers=viewer_headers,
            json={"name": "Nope"},
        )
    ).status_code == 403


async def test_manual_enrich_endpoint_refreshes_profile_and_rejects_unprocessed(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ws_id = entities.finance.id
    doc_id = await _upload_pdf(
        client, auth_headers, ws_id, "HR_Leave_Policy_v2.pdf", "Annual leave policy"
    )

    # Without an active processing run, enrich returns 409
    not_ready = await client.post(
        f"/v1/workspaces/{ws_id}/documents/{doc_id}/enrich",
        headers=auth_headers,
    )
    assert not_ready.status_code == 409

    # Seed an active run + chunk so enrich can build a basic profile (no model configured)
    async with db_session_factory() as session, session.begin():
        run = ProcessingRun(
            document_id=doc_id,
            origin_workspace_id=ws_id,
            job_kind=JOB_EXTRACT,
            pipeline_version="2026.1",
            status="ready",
            chunk_count=1,
            embedded_chunk_count=1,
        )
        session.add(run)
        await session.flush()
        doc = await session.get(Document, doc_id)
        assert doc is not None
        doc.active_processing_run_id = run.id
        text = "Employee Annual Leave Policy effective 2026-01-01."
        session.add(
            DocumentChunk(
                origin_workspace_id=ws_id,
                document_id=doc_id,
                processing_run_id=run.id,
                ordinal=0,
                text=text,
                token_count=7,
                section_path=["Leave Policy"],
                section_key="leave-policy",
                page_numbers=[1],
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
            )
        )

    enriched = await client.post(
        f"/v1/workspaces/{ws_id}/documents/{doc_id}/enrich",
        headers=auth_headers,
    )
    assert enriched.status_code == 200, enriched.text
    payload = enriched.json()
    assert payload["profile"] is not None
    assert payload["profile"]["status"] == "basic"
    assert payload["profile"]["doc_category"] == "policy_sop"
