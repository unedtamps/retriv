from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import RefreshToken, User
from app.security.auth import create_access_token
from app.security.refresh_tokens import (
    hash_refresh_token,
    issue_refresh_token,
    looks_like_refresh_token,
)
from tests.conftest import SeededEntities

PASSWORD = "correct-horse-battery-staple"


async def login(client: httpx.AsyncClient, username: str = "alice") -> dict[str, Any]:
    response = await client.post(
        "/v1/auth/token",
        data={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def refresh(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.post("/v1/auth/refresh", json={"refresh_token": token})


async def test_login_returns_a_usable_refresh_token(
    client: httpx.AsyncClient, entities: SeededEntities
) -> None:
    body = await login(client)

    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    assert body["refresh_expires_in"] == 30 * 24 * 3600
    assert looks_like_refresh_token(body["refresh_token"])
    assert body["refresh_token"].startswith("rt_")
    assert body["refresh_token"] != body["access_token"]


async def test_refresh_returns_an_access_token_that_works(
    client: httpx.AsyncClient, entities: SeededEntities
) -> None:
    body = await login(client)

    response = await refresh(client, body["refresh_token"])

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    rotated = response.json()
    assert rotated["access_token"] != body["access_token"]
    assert rotated["refresh_token"] != body["refresh_token"]

    profile = await client.get(
        "/v1/auth/me", headers={"Authorization": f"Bearer {rotated['access_token']}"}
    )
    assert profile.status_code == 200
    assert profile.json()["username"] == entities.user.username


async def test_a_rotated_token_stops_working(
    client: httpx.AsyncClient, entities: SeededEntities
) -> None:
    del entities
    body = await login(client)
    first = await refresh(client, body["refresh_token"])
    assert first.status_code == 200

    replay = await refresh(client, body["refresh_token"])

    assert replay.status_code == 401
    assert replay.json()["code"] == "invalid_credentials"


async def test_reuse_kills_the_whole_family(
    client: httpx.AsyncClient, entities: SeededEntities
) -> None:
    del entities
    """A replayed token means a copy leaked, so no chain member survives."""
    body = await login(client)
    first = await refresh(client, body["refresh_token"])
    assert first.status_code == 200
    current = first.json()["refresh_token"]

    replayed = await refresh(client, body["refresh_token"])
    assert replayed.status_code == 401

    after = await refresh(client, current)
    assert after.status_code == 401


async def test_an_unknown_or_malformed_token_is_refused(
    client: httpx.AsyncClient, entities: SeededEntities
) -> None:
    del entities
    unknown = await refresh(client, f"rt_{'a' * 43}")
    malformed = await refresh(client, "not-a-token")

    assert unknown.status_code == 401
    assert malformed.status_code == 401
    assert unknown.json()["code"] == "invalid_credentials"


async def test_an_access_token_is_not_a_refresh_token(
    client: httpx.AsyncClient, entities: SeededEntities, settings: Any
) -> None:
    access_token, _ = create_access_token(entities.user, settings)

    response = await refresh(client, access_token)

    assert response.status_code == 401


async def test_a_refresh_token_is_not_a_bearer_token(
    client: httpx.AsyncClient, entities: SeededEntities
) -> None:
    del entities
    body = await login(client)

    response = await client.get(
        "/v1/auth/me", headers={"Authorization": f"Bearer {body['refresh_token']}"}
    )

    assert response.status_code == 401


async def test_an_expired_token_is_refused(
    client: httpx.AsyncClient,
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    del entities
    body = await login(client)
    async with db_session_factory() as session:
        record = await session.scalar(
            select(RefreshToken).where(
                RefreshToken.token_hash == hash_refresh_token(body["refresh_token"])
            )
        )
        assert record is not None
        record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    response = await refresh(client, body["refresh_token"])

    assert response.status_code == 401


async def test_a_password_reset_closes_existing_chains(
    client: httpx.AsyncClient,
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    body = await login(client)
    async with db_session_factory() as session:
        user = await session.get(User, entities.user.id)
        assert user is not None
        user.auth_version += 1
        await session.commit()

    response = await refresh(client, body["refresh_token"])

    assert response.status_code == 401


async def test_a_deactivated_user_cannot_refresh(
    client: httpx.AsyncClient,
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    body = await login(client)
    async with db_session_factory() as session:
        user = await session.get(User, entities.user.id)
        assert user is not None
        user.is_active = False
        await session.commit()

    response = await refresh(client, body["refresh_token"])

    assert response.status_code == 401


async def test_logout_revokes_the_session_and_is_idempotent(
    client: httpx.AsyncClient, entities: SeededEntities
) -> None:
    del entities
    body = await login(client)

    first = await client.post("/v1/auth/logout", json={"refresh_token": body["refresh_token"]})
    second = await client.post("/v1/auth/logout", json={"refresh_token": body["refresh_token"]})
    reuse = await refresh(client, body["refresh_token"])

    assert first.status_code == 204
    assert second.status_code == 204
    assert reuse.status_code == 401


async def test_logout_of_an_unknown_token_is_still_204(
    client: httpx.AsyncClient, entities: SeededEntities
) -> None:
    del entities
    response = await client.post("/v1/auth/logout", json={"refresh_token": f"rt_{'b' * 43}"})

    assert response.status_code == 204


async def test_only_hashes_are_stored(
    client: httpx.AsyncClient,
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    del entities
    body = await login(client)
    async with db_session_factory() as session:
        stored = (await session.execute(select(RefreshToken))).scalars().all()

    assert len(stored) == 1
    assert stored[0].token_hash == hash_refresh_token(body["refresh_token"])
    assert body["refresh_token"] not in {record.token_hash for record in stored}


async def test_the_oldest_families_are_revoked_past_the_cap(
    client: httpx.AsyncClient,
    entities: SeededEntities,
    settings: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.security.refresh_tokens import MAX_ACTIVE_FAMILIES, trim_active_families

    async with db_session_factory() as session:
        user = await session.get(User, entities.user.id)
        assert user is not None
        for _ in range(MAX_ACTIVE_FAMILIES + 3):
            await issue_refresh_token(session, user, settings)
        await session.commit()

        await trim_active_families(session, user.id)
        await session.commit()

        live = (
            (await session.execute(select(RefreshToken).where(RefreshToken.revoked_at.is_(None))))
            .scalars()
            .all()
        )

    assert len({record.family_id for record in live}) == MAX_ACTIVE_FAMILIES


async def test_a_login_issues_a_fresh_family(
    client: httpx.AsyncClient,
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    del entities
    first = await login(client)
    second = await login(client)

    async with db_session_factory() as session:
        rows = (await session.execute(select(RefreshToken))).scalars().all()
        by_hash = {record.token_hash: record for record in rows}

    first_family = by_hash[hash_refresh_token(first["refresh_token"])].family_id
    second_family = by_hash[hash_refresh_token(second["refresh_token"])].family_id
    assert first_family != second_family
    assert first_family != uuid4()
