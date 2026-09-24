import httpx

from tests.conftest import SeededEntities


async def test_authenticated_user_can_list_all_workspaces(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    response = await client.get("/v1/workspaces", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert {item["id"] for item in body["items"]} == {
        str(entities.finance.id),
        str(entities.legal.id),
    }


async def test_workspace_list_reports_download_permission_for_each_role(
    client: httpx.AsyncClient,
    entities: SeededEntities,
) -> None:
    owner_login = await client.post(
        "/v1/auth/token",
        data={"username": "alice", "password": "correct-horse-battery-staple"},
    )
    viewer_login = await client.post(
        "/v1/auth/token",
        data={"username": "bob", "password": "another-correct-password"},
    )

    owner_response = await client.get(
        "/v1/workspaces",
        headers={"Authorization": f"Bearer {owner_login.json()['access_token']}"},
    )
    viewer_response = await client.get(
        "/v1/workspaces",
        headers={"Authorization": f"Bearer {viewer_login.json()['access_token']}"},
    )

    assert owner_response.status_code == viewer_response.status_code == 200
    owner_workspaces = {item["id"]: item for item in owner_response.json()["items"]}
    viewer_workspaces = {item["id"]: item for item in viewer_response.json()["items"]}
    assert owner_workspaces[str(entities.finance.id)]["can_download"] is True
    assert viewer_workspaces[str(entities.legal.id)]["can_search_context"] is True
    assert viewer_workspaces[str(entities.legal.id)]["can_download"] is False


async def test_workspace_list_is_paginated(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    del entities
    response = await client.get("/v1/workspaces?offset=0&limit=1", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert len(response.json()["items"]) == 1
