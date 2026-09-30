from uuid import UUID

import httpx

from tests.conftest import SeededEntities
from tests.document_samples import pdf_bytes


async def test_admin_can_create_user_and_return_password_once(
    client: httpx.AsyncClient,
    entities: SeededEntities,
) -> None:
    admin_headers = await login_headers(client, "admin", "admin-correct-password")

    created = await client.post(
        "/v1/admin/users",
        headers=admin_headers,
        json={"username": "carol", "role": "user"},
    )

    assert created.status_code == 201
    body = created.json()
    assert body["username"] == "carol"
    assert body["initial_password"]
    assert created.headers["cache-control"] == "no-store"

    login = await client.post(
        "/v1/auth/token",
        data={"username": "carol", "password": body["initial_password"]},
    )
    assert login.status_code == 200

    duplicate = await client.post(
        "/v1/admin/users",
        headers=admin_headers,
        json={"username": "carol", "role": "user"},
    )
    assert duplicate.status_code == 409
    assert "initial_password" not in duplicate.json()
    assert UUID(body["id"]) != entities.admin_user.id


async def test_current_user_profile_returns_authenticated_identity(
    client: httpx.AsyncClient,
    entities: SeededEntities,
) -> None:
    headers = await login_headers(client, "admin", "admin-correct-password")

    response = await client.get("/v1/auth/me", headers=headers)

    assert response.status_code == 200
    assert response.json() == {
        "id": str(entities.admin_user.id),
        "username": "admin",
        "role": "admin",
        "is_active": True,
    }


async def test_admin_can_search_users_without_password_data(
    client: httpx.AsyncClient,
    entities: SeededEntities,
) -> None:
    headers = await login_headers(client, "admin", "admin-correct-password")

    response = await client.get("/v1/admin/users?search=ali", headers=headers)

    assert response.status_code == 200
    assert response.json()["total"] == 1
    item = response.json()["items"][0]
    assert item["id"] == str(entities.user.id)
    assert item["username"] == "alice"
    assert item["role"] == "user"
    assert item["is_active"] is True
    assert item["created_at"]
    assert "password_hash" not in response.text


async def test_admin_can_create_workspace_and_assign_owner(
    client: httpx.AsyncClient,
    entities: SeededEntities,
) -> None:
    admin_headers = await login_headers(client, "admin", "admin-correct-password")

    response = await client.post(
        "/v1/admin/workspaces",
        headers=admin_headers,
        json={
            "slug": "engineering",
            "name": "Engineering",
            "owner_user_id": str(entities.user.id),
        },
    )

    assert response.status_code == 201
    assert response.json()["slug"] == "engineering"

    members = await client.get(
        f"/v1/workspaces/{response.json()['id']}/members",
        headers=await login_headers(client, "alice", "correct-horse-battery-staple"),
    )
    assert members.status_code == 200
    assert members.json()["items"][0]["role"] == "owner"


async def test_admin_without_membership_cannot_upload(
    client: httpx.AsyncClient,
    entities: SeededEntities,
) -> None:
    admin_headers = await login_headers(client, "admin", "admin-correct-password")

    response = await client.post(
        f"/v1/workspaces/{entities.finance.id}/documents",
        headers=admin_headers,
        files={"file": ("report.pdf", pdf_bytes(), "application/pdf")},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "workspace_not_found"


async def test_regular_user_can_see_catalog_but_not_unassigned_documents(
    client: httpx.AsyncClient,
    entities: SeededEntities,
) -> None:
    viewer_headers = await login_headers(client, "bob", "another-correct-password")

    catalog = await client.get("/v1/workspaces", headers=viewer_headers)
    assert catalog.status_code == 200
    by_slug = {item["slug"]: item for item in catalog.json()["items"]}
    assert by_slug["legal"]["membership_role"] == "viewer"
    assert by_slug["finance"]["membership_role"] is None

    documents = await client.get(
        f"/v1/workspaces/{entities.finance.id}/documents",
        headers=viewer_headers,
    )
    assert documents.status_code == 404
    assert documents.json()["code"] == "workspace_not_found"


async def test_regular_user_cannot_use_admin_dashboard(
    client: httpx.AsyncClient,
    entities: SeededEntities,
) -> None:
    del entities
    user_headers = await login_headers(client, "alice", "correct-horse-battery-staple")
    response = await client.post(
        "/v1/admin/users",
        headers=user_headers,
        json={"username": "not-an-admin"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "admin_required"


async def test_owner_can_add_viewer_but_viewer_cannot_upload(
    client: httpx.AsyncClient,
    entities: SeededEntities,
) -> None:
    owner_headers = await login_headers(client, "alice", "correct-horse-battery-staple")
    response = await client.put(
        f"/v1/workspaces/{entities.finance.id}/members/{entities.second_user.id}",
        headers=owner_headers,
        json={"role": "viewer"},
    )
    assert response.status_code == 200
    assert response.json()["role"] == "viewer"

    viewer_headers = await login_headers(client, "bob", "another-correct-password")
    upload = await client.post(
        f"/v1/workspaces/{entities.finance.id}/documents",
        headers=viewer_headers,
        files={"file": ("report.pdf", pdf_bytes(), "application/pdf")},
    )
    assert upload.status_code == 403
    assert upload.json()["code"] == "insufficient_workspace_permission"


async def test_owner_can_search_only_nonmember_candidates(
    client: httpx.AsyncClient,
    entities: SeededEntities,
) -> None:
    owner_headers = await login_headers(client, "alice", "correct-horse-battery-staple")

    response = await client.get(
        f"/v1/workspaces/{entities.finance.id}/member-candidates?search=bo",
        headers=owner_headers,
    )

    assert response.status_code == 200
    assert response.json()["items"] == [
        {"id": str(entities.second_user.id), "username": "bob"}
    ]

    existing_member = await client.get(
        f"/v1/workspaces/{entities.finance.id}/member-candidates?search=ali",
        headers=owner_headers,
    )
    assert existing_member.status_code == 200
    assert existing_member.json()["items"] == []


async def test_owner_can_add_editor_and_editor_can_upload(
    client: httpx.AsyncClient,
    entities: SeededEntities,
) -> None:
    owner_headers = await login_headers(client, "alice", "correct-horse-battery-staple")
    response = await client.put(
        f"/v1/workspaces/{entities.finance.id}/members/{entities.second_user.id}",
        headers=owner_headers,
        json={"role": "editor"},
    )
    assert response.status_code == 200

    editor_headers = await login_headers(client, "bob", "another-correct-password")
    upload = await client.post(
        f"/v1/workspaces/{entities.finance.id}/documents",
        headers=editor_headers,
        files={"file": ("report.pdf", pdf_bytes(), "application/pdf")},
    )
    assert upload.status_code == 202


async def test_owner_cannot_assign_another_owner(
    client: httpx.AsyncClient,
    entities: SeededEntities,
) -> None:
    owner_headers = await login_headers(client, "alice", "correct-horse-battery-staple")
    response = await client.put(
        f"/v1/workspaces/{entities.finance.id}/members/{entities.second_user.id}",
        headers=owner_headers,
        json={"role": "owner"},
    )

    assert response.status_code == 422


async def test_admin_cannot_demote_or_remove_last_owner(
    client: httpx.AsyncClient,
    entities: SeededEntities,
) -> None:
    admin_headers = await login_headers(client, "admin", "admin-correct-password")

    demote = await client.put(
        f"/v1/admin/workspaces/{entities.finance.id}/members/{entities.user.id}",
        headers=admin_headers,
        json={"role": "editor"},
    )
    assert demote.status_code == 409
    assert demote.json()["code"] == "last_owner_required"

    remove = await client.delete(
        f"/v1/admin/workspaces/{entities.finance.id}/members/{entities.user.id}",
        headers=admin_headers,
    )
    assert remove.status_code == 409
    assert remove.json()["code"] == "last_owner_required"


async def test_viewer_cannot_manage_workspace_members(
    client: httpx.AsyncClient,
    entities: SeededEntities,
) -> None:
    viewer_headers = await login_headers(client, "bob", "another-correct-password")
    response = await client.put(
        f"/v1/workspaces/{entities.legal.id}/members/{entities.user.id}",
        headers=viewer_headers,
        json={"role": "editor"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "insufficient_workspace_permission"


async def test_password_reset_invalidates_existing_tokens(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    admin_headers = await login_headers(client, "admin", "admin-correct-password")
    reset = await client.post(
        f"/v1/admin/users/{entities.user.id}/password",
        headers=admin_headers,
    )

    assert reset.status_code == 200
    assert reset.headers["cache-control"] == "no-store"
    assert (await client.get("/v1/workspaces", headers=auth_headers)).status_code == 401
    assert (
        await client.post(
            "/v1/auth/token",
            data={"username": "alice", "password": reset.json()["initial_password"]},
        )
    ).status_code == 200


async def login_headers(
    client: httpx.AsyncClient, username: str, password: str
) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/token",
        data={"username": username, "password": password},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}
