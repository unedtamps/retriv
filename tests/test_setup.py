from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.models import SystemState, User
from app.security.auth import hash_password
from tests.conftest import SeededEntities

PASSWORD = "setup-password-123"


async def test_setup_status_offers_the_first_admin(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/setup/status")

    assert response.status_code == 200
    assert response.json() == {"needs_admin": True, "setup_allowed": True}


async def test_first_admin_is_created_once_and_can_sign_in(
    client: httpx.AsyncClient,
) -> None:
    created = await client.post(
        "/v1/setup/admin",
        json={"username": "root", "password": PASSWORD},
    )

    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    body = created.json()
    assert body["access_token"]
    assert body["refresh_token"]

    me = await client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert me.status_code == 200
    assert me.json()["username"] == "root"
    assert me.json()["role"] == "admin"

    status = await client.get("/v1/setup/status")
    assert status.json() == {"needs_admin": False, "setup_allowed": True}

    login = await client.post(
        "/v1/auth/token",
        data={"username": "root", "password": PASSWORD},
    )
    assert login.status_code == 200

    second = await client.post(
        "/v1/setup/admin",
        json={"username": "other", "password": PASSWORD},
    )
    assert second.status_code == 409
    assert second.json()["code"] == "setup_already_completed"


async def test_an_existing_admin_closes_setup(
    client: httpx.AsyncClient, entities: SeededEntities
) -> None:
    del entities

    status = await client.get("/v1/setup/status")
    assert status.json() == {"needs_admin": False, "setup_allowed": True}

    response = await client.post(
        "/v1/setup/admin",
        json={"username": "root", "password": PASSWORD},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "setup_already_completed"


async def test_an_existing_username_does_not_consume_the_setup(
    client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        session.add(
            User(
                username="root",
                password_hash=hash_password("existing-password-123"),
                role="user",
            )
        )
        await session.commit()

    conflict = await client.post(
        "/v1/setup/admin",
        json={"username": "root", "password": PASSWORD},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "username_taken"

    status = await client.get("/v1/setup/status")
    assert status.json()["needs_admin"] is True

    created = await client.post(
        "/v1/setup/admin",
        json={"username": "root-admin", "password": PASSWORD},
    )
    assert created.status_code == 201


async def test_web_setup_can_be_disabled(
    client: httpx.AsyncClient,
    app: Any,
    settings: Settings,
) -> None:
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"allow_web_setup": False}
    )

    status = await client.get("/v1/setup/status")
    assert status.json() == {"needs_admin": True, "setup_allowed": False}

    response = await client.post(
        "/v1/setup/admin",
        json={"username": "root", "password": PASSWORD},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "setup_disabled"


async def test_setup_records_the_gate(
    client: httpx.AsyncClient,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await client.post(
        "/v1/setup/admin",
        json={"username": "root", "password": PASSWORD},
    )

    async with db_session_factory() as session:
        state = await session.scalar(select(SystemState).where(SystemState.id == 1))
        assert state is not None
        assert state.admin_created_at is not None
