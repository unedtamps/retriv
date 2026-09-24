import httpx
import pytest

from dummy_receiver.main import app, received_events


@pytest.mark.asyncio
async def test_dummy_receiver_accepts_and_deduplicates_events() -> None:
    received_events.clear()
    payload = {
        "id": "6d9f6a9d-1b8c-4d5f-8c3d-8a8f2d7b4c11",
        "type": "document.ready",
        "source": "test",
        "subject": "document/6d9f6a9d-1b8c-4d5f-8c3d-8a8f2d7b4c11",
        "schema_version": 1,
        "occurred_at": "2026-09-21T12:00:00Z",
        "data": {"document_id": "6d9f6a9d-1b8c-4d5f-8c3d-8a8f2d7b4c11"},
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/events", json=payload)
        second = await client.post("/events", json=payload)
        listed = await client.get("/events")

    assert first.status_code == 202
    assert first.json()["duplicate"] is False
    assert second.status_code == 202
    assert second.json()["duplicate"] is True
    assert listed.json()["total"] == 1
    received_events.clear()
