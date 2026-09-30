from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from sqlalchemy import select

from app.db.models import (
    RUN_EMBEDDING,
    RUN_FETCHING,
    RUN_QUEUED,
    RUN_READY,
    RUN_RETRY_WAIT,
    ProcessingRun,
)
from tests.conftest import ServiceContext, ready_event, send_event


async def test_active_endpoint_returns_latest_nonterminal_run_in_requested_workspaces(
    client: httpx.AsyncClient,
    context: ServiceContext,
) -> None:
    first_workspace = uuid4()
    second_workspace = uuid4()
    excluded_workspace = uuid4()
    events = [
        ready_event(b"first", workspace_id=first_workspace, filename="first.pdf"),
        ready_event(b"second", workspace_id=second_workspace, filename="second.pdf"),
        ready_event(b"excluded", workspace_id=excluded_workspace, filename="excluded.pdf"),
    ]
    for event in events:
        accepted = await send_event(client, context.settings, event)
        assert accepted.status_code == 202

    async with context.session_factory() as session:
        runs = list((await session.scalars(select(ProcessingRun))).all())
        by_document = {str(run.document_id): run for run in runs}
        by_document[events[0]["data"]["document_id"]].status = RUN_EMBEDDING
        by_document[events[1]["data"]["document_id"]].status = RUN_RETRY_WAIT
        by_document[events[2]["data"]["document_id"]].status = RUN_FETCHING
        by_document[events[0]["data"]["document_id"]].created_at = (
            datetime.now(UTC) - timedelta(minutes=3)
        )
        by_document[events[1]["data"]["document_id"]].created_at = (
            datetime.now(UTC) - timedelta(minutes=1)
        )
        by_document[events[2]["data"]["document_id"]].created_at = datetime.now(UTC)
        await session.commit()

    response = await client.post(
        "/internal/processing-runs/active",
        json={"workspace_ids": [str(first_workspace), str(second_workspace)]},
        headers={"Authorization": f"Bearer {context.settings.api_token.get_secret_value()}"},
    )

    assert response.status_code == 200
    item = response.json()["item"]
    assert item["workspace_id"] == str(second_workspace)
    assert item["document_id"] == events[1]["data"]["document_id"]
    assert item["filename"] == "second.pdf"
    assert item["status"] == RUN_RETRY_WAIT


async def test_recent_endpoint_returns_all_statuses_since_one_hour_newest_first(
    client: httpx.AsyncClient,
    context: ServiceContext,
) -> None:
    workspace_id = uuid4()
    other_workspace_id = uuid4()
    recent_active = ready_event(b"active", workspace_id=workspace_id, filename="active.pdf")
    recent_complete = ready_event(b"done", workspace_id=workspace_id, filename="done.pdf")
    old_event = ready_event(b"old", workspace_id=workspace_id, filename="old.pdf")
    other_event = ready_event(b"other", workspace_id=other_workspace_id, filename="other.pdf")
    for event in (recent_active, recent_complete, old_event, other_event):
        accepted = await send_event(client, context.settings, event)
        assert accepted.status_code == 202

    async with context.session_factory() as session:
        runs = list((await session.scalars(select(ProcessingRun))).all())
        by_document = {str(run.document_id): run for run in runs}
        now = datetime.now(UTC)
        by_document[recent_active["data"]["document_id"]].status = RUN_QUEUED
        by_document[recent_active["data"]["document_id"]].created_at = now - timedelta(minutes=1)
        by_document[recent_complete["data"]["document_id"]].status = RUN_READY
        by_document[recent_complete["data"]["document_id"]].created_at = now
        by_document[old_event["data"]["document_id"]].status = RUN_READY
        by_document[old_event["data"]["document_id"]].created_at = now - timedelta(hours=2)
        by_document[other_event["data"]["document_id"]].status = RUN_READY
        await session.commit()

    response = await client.post(
        "/internal/processing-runs/recent",
        json={"workspace_ids": [str(workspace_id)]},
        headers={"Authorization": f"Bearer {context.settings.api_token.get_secret_value()}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [item["document_id"] for item in body["items"]] == [
        recent_complete["data"]["document_id"],
        recent_active["data"]["document_id"],
    ]
    assert [item["status"] for item in body["items"]] == [RUN_READY, RUN_QUEUED]
    assert body["items"][0]["filename"] == "done.pdf"
    assert datetime.fromisoformat(body["since"]) > datetime.now(UTC) - timedelta(hours=1, minutes=1)


async def test_processing_run_endpoints_require_internal_service_credentials(
    client: httpx.AsyncClient,
) -> None:
    workspace_id = uuid4()

    active = await client.post(
        "/internal/processing-runs/active",
        json={"workspace_ids": [str(workspace_id)]},
    )
    recent = await client.post(
        "/internal/processing-runs/recent",
        json={"workspace_ids": [str(workspace_id)]},
    )

    assert active.status_code == 401
    assert recent.status_code == 401
