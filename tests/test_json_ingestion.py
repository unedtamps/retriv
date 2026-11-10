import json
from uuid import uuid4

import httpx

from app.retrieval.catalogue import format_values
from app.retrieval.sql import ROW_STORED_MEDIA_TYPES, RowResult
from tests.conftest import SeededEntities


def retriv_payload() -> dict[str, object]:
    return {
        "protocol": "retriv-json",
        "version": 1,
        "tables": [
            {
                "name": "orders",
                "columns": [
                    {"name": "id", "type": "integer", "nullable": False},
                    {"name": "total", "type": "number"},
                ],
                "records": [
                    {"id": 1001, "total": 42.5},
                    {"id": 1002, "total": 17},
                ],
            }
        ],
    }


async def upload_json(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    workspace_id: str,
    payload: object | None = None,
    *,
    filename: str = "orders.json",
) -> httpx.Response:
    content = json.dumps(payload if payload is not None else retriv_payload()).encode()
    return await client.post(
        f"/v1/workspaces/{workspace_id}/documents",
        headers=headers,
        files={"file": (filename, content, "application/json")},
    )


async def post_json(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    workspace_id: str,
    payload: object | None = None,
    *,
    filename: str | None = None,
) -> httpx.Response:
    url = f"/v1/workspaces/{workspace_id}/documents/json"
    if filename is not None:
        url = f"{url}?filename={filename}"
    return await client.post(
        url,
        headers={**headers, "content-type": "application/json"},
        content=json.dumps(payload if payload is not None else retriv_payload()).encode(),
    )


async def test_a_protocol_json_upload_is_accepted(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    response = await upload_json(client, auth_headers, str(entities.finance.id))

    assert response.status_code == 202
    body = response.json()
    assert body["detected_media_type"] == "application/json"
    assert body["original_filename"] == "orders.json"
    assert body["duplicate"] is False


async def test_a_duplicate_json_upload_returns_the_existing_document(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    first = await upload_json(client, auth_headers, str(entities.finance.id))
    second = await upload_json(client, auth_headers, str(entities.finance.id))

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["id"] == first.json()["id"]


async def test_raw_json_is_rejected_with_the_protocol_message(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    response = await upload_json(
        client,
        auth_headers,
        str(entities.finance.id),
        [{"id": 1}],
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "invalid_json_protocol"
    assert "protocol" in body["detail"]


async def test_a_direct_json_post_creates_the_same_document(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    response = await post_json(client, auth_headers, str(entities.finance.id))

    assert response.status_code == 202
    body = response.json()
    assert body["detected_media_type"] == "application/json"
    assert body["original_filename"] == "data.json"
    assert body["duplicate"] is False


async def test_a_file_and_a_post_with_the_same_payload_deduplicate(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    uploaded = await upload_json(client, auth_headers, str(entities.finance.id))
    posted = await post_json(client, auth_headers, str(entities.finance.id))

    assert uploaded.status_code == 202
    assert posted.status_code == 200
    assert posted.json()["duplicate"] is True
    assert posted.json()["id"] == uploaded.json()["id"]


async def test_direct_json_post_requires_json_content_type(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    response = await client.post(
        f"/v1/workspaces/{entities.finance.id}/documents/json",
        headers={**auth_headers, "content-type": "text/plain"},
        content=b"{}",
    )

    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_media_type"


async def test_direct_json_post_rejects_an_invalid_payload(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    response = await post_json(
        client,
        auth_headers,
        str(entities.finance.id),
        {"protocol": "retriv-json", "version": 1, "tables": []},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_json_protocol"


async def test_direct_json_post_appends_a_json_suffix(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    response = await post_json(
        client,
        auth_headers,
        str(entities.finance.id),
        filename="orders",
    )

    assert response.status_code == 202
    assert response.json()["original_filename"] == "orders.json"


async def test_direct_json_post_rejects_a_non_json_suffix(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    response = await post_json(
        client,
        auth_headers,
        str(entities.finance.id),
        filename="orders.csv",
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_filename"



def test_json_nested_cells_render_as_json_in_citations() -> None:
    result = RowResult(
        chunk_id=uuid4(),
        workspace_id=uuid4(),
        document_id=uuid4(),
        processing_run_id=uuid4(),
        row_number=1,
        data={"customer": {"name": "A"}, "tags": ["x", "y"]},
        columns=["customer", "tags"],
        sheet="orders",
    )

    text = result.as_source("orders.json").text
    assert 'customer={"name":"A"}' in text
    assert 'tags=["x","y"]' in text
    assert "application/json" in ROW_STORED_MEDIA_TYPES


def test_computed_json_values_render_as_json() -> None:
    text = format_values([{"customer": {"name": "A"}, "count": 2}])
    assert '{"name":"A"}' in text
    assert "2" in text


async def test_direct_json_post_rejects_an_api_key(
    client: httpx.AsyncClient,
    entities: SeededEntities,
) -> None:
    response = await client.post(
        f"/v1/workspaces/{entities.finance.id}/documents/json",
        headers={
            "authorization": f"Bearer rk_{'a' * 43}",
            "content-type": "application/json",
        },
        content=json.dumps(retriv_payload()).encode(),
    )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"


async def test_direct_json_post_requires_upload_permission(
    client: httpx.AsyncClient,
    entities: SeededEntities,
) -> None:
    login = await client.post(
        "/v1/auth/token",
        data={"username": "bob", "password": "another-correct-password"},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = await post_json(
        client,
        headers,
        str(entities.legal.id),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "insufficient_workspace_permission"
