from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.dependencies import get_bearer_user
from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import ApiKey, User
from app.security.api_keys import (
    authenticate_api_key,
    bearer_api_key,
    generate_api_key,
    hash_api_key,
    key_prefix_of,
    looks_like_api_key,
    mint_api_key,
)
from tests.conftest import SeededEntities


def test_generated_keys_are_well_formed_and_distinct() -> None:
    keys = {generate_api_key() for _ in range(50)}

    assert len(keys) == 50
    for key in keys:
        assert looks_like_api_key(key)
        assert len(key) == len("rk_") + 43
        assert key_prefix_of(key) == key[:11]


def test_hash_is_stable_and_key_scoped() -> None:
    key = generate_api_key()

    assert hash_api_key(key) == hash_api_key(key)
    assert hash_api_key(key) != hash_api_key(generate_api_key())
    assert key not in hash_api_key(key)


async def test_mint_stores_hash_not_plaintext(
    db_session_factory: async_sessionmaker[AsyncSession],
    entities: SeededEntities,
) -> None:
    async with db_session_factory() as session:
        minted = await mint_api_key(
            session, user_id=entities.user.id, name="ci-agent"
        )
        await session.commit()

        stored = await session.get(ApiKey, minted.record.id)
        assert stored is not None
        assert stored.key_hash == hash_api_key(minted.plaintext)
        assert stored.key_hash != minted.plaintext
        assert stored.key_prefix == minted.plaintext[:11]
        assert stored.revoked_at is None
        assert stored.user_id == entities.user.id


async def test_authenticate_resolves_owner_and_rejects_junk(
    db_session_factory: async_sessionmaker[AsyncSession],
    entities: SeededEntities,
) -> None:
    async with db_session_factory() as session:
        minted = await mint_api_key(
            session, user_id=entities.user.id, name="local"
        )
        await session.commit()

        user = await authenticate_api_key(session, minted.plaintext)
        assert user is not None
        assert user.id == entities.user.id

        assert await authenticate_api_key(session, "not-a-key") is None
        assert await authenticate_api_key(session, generate_api_key()) is None
        assert await authenticate_api_key(session, "") is None


async def test_revoked_key_authenticates_no_more(
    db_session_factory: async_sessionmaker[AsyncSession],
    entities: SeededEntities,
) -> None:
    async with db_session_factory() as session:
        minted = await mint_api_key(
            session, user_id=entities.user.id, name="to-revoke"
        )
        record = await session.get(ApiKey, minted.record.id)
        assert record is not None
        record.revoked_at = datetime.now(UTC)
        await session.commit()

        assert await authenticate_api_key(session, minted.plaintext) is None


async def test_deactivating_owner_kills_their_keys(
    db_session_factory: async_sessionmaker[AsyncSession],
    entities: SeededEntities,
) -> None:
    async with db_session_factory() as session:
        minted = await mint_api_key(session, user_id=entities.user.id, name="a")
        await session.commit()

    async with db_session_factory() as session:
        owner = await session.get(User, entities.user.id)
        assert owner is not None
        owner.is_active = False
        await session.commit()

    async with db_session_factory() as session:
        assert await authenticate_api_key(session, minted.plaintext) is None


async def test_listing_never_returns_key_material(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    created = await client.post(
        "/v1/api-keys", json={"name": "dashboard key"}, headers=auth_headers
    )
    assert created.status_code == 201
    plaintext = created.json()["key"]

    listed = await client.get("/v1/api-keys", headers=auth_headers)
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 1
    assert plaintext not in listed.text
    assert items[0]["key_prefix"] == plaintext[:11]
    assert items[0]["last_used_at"] is None
    assert items[0]["revoked_at"] is None


async def test_created_key_is_returned_once_with_warning(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/v1/api-keys", json={"name": "  spaced  "}, headers=auth_headers
    )

    assert response.status_code == 201
    body = response.json()
    assert looks_like_api_key(body["key"])
    assert body["name"] == "spaced"
    assert response.headers["cache-control"] == "no-store"
    assert "only time" in body["warning"]


async def test_blank_name_is_rejected(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/v1/api-keys", json={"name": "   "}, headers=auth_headers
    )

    assert response.status_code == 422


async def test_revoke_requires_authentication(client: httpx.AsyncClient) -> None:
    response = await client.delete("/v1/api-keys/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 401


async def test_revoke_removes_the_key_and_is_idempotent_for_the_owner(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    created = await client.post(
        "/v1/api-keys", json={"name": "revoke me"}, headers=auth_headers
    )
    key_id = created.json()["id"]

    first = await client.delete(f"/v1/api-keys/{key_id}", headers=auth_headers)
    assert first.status_code == 204

    second = await client.delete(f"/v1/api-keys/{key_id}", headers=auth_headers)
    assert second.status_code == 404


async def test_cannot_revoke_another_users_key(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    created = await client.post(
        "/v1/api-keys", json={"name": "alice key"}, headers=auth_headers
    )
    key_id = created.json()["id"]

    # Bob's credentials: the key exists, but it is not his.
    login = await client.post(
        "/v1/auth/token",
        data={"username": "bob", "password": "another-correct-password"},
    )
    bob_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = await client.delete(f"/v1/api-keys/{key_id}", headers=bob_headers)

    assert response.status_code == 404
    listed = await client.get("/v1/api-keys", headers=auth_headers)
    assert listed.json()["items"][0]["revoked_at"] is None


async def test_active_key_limit_is_enforced(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    from app.routes.api_keys import MAX_KEYS_PER_USER

    for index in range(MAX_KEYS_PER_USER):
        response = await client.post(
            "/v1/api-keys", json={"name": f"key-{index}"}, headers=auth_headers
        )
        assert response.status_code == 201

    overflow = await client.post(
        "/v1/api-keys", json={"name": "one-too-many"}, headers=auth_headers
    )

    assert overflow.status_code == 409
    assert overflow.json()["code"] == "api_key_limit_reached"


async def test_minted_key_resolves_to_owner_through_dependency(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    db_session_factory: async_sessionmaker[AsyncSession],
    entities: SeededEntities,
) -> None:
    """End-to-end: mint over REST, then authenticate the plaintext."""
    created = await client.post(
        "/v1/api-keys", json={"name": "mcp"}, headers=auth_headers
    )
    plaintext = created.json()["key"]

    async with db_session_factory() as session:
        user = await authenticate_api_key(
            session, bearer_api_key(f"Bearer {plaintext}")
        )
    assert user is not None
    assert user.username == "alice"


async def test_bearer_header_parser_rejects_malformed_values() -> None:
    for header in (None, "", "Basic abc", "Bearer", "bearer  ", "rk_nope"):
        with pytest.raises(AppError) as excinfo:
            bearer_api_key(header)
        assert excinfo.value.status_code == 401


async def test_inactive_user_cannot_mint_keys(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    db_session_factory: async_sessionmaker[AsyncSession],
    entities: SeededEntities,
) -> None:
    entities.user.is_active = False
    async with db_session_factory() as session:
        await session.merge(entities.user)
        await session.commit()

    response = await client.post(
        "/v1/api-keys", json={"name": "nope"}, headers=auth_headers
    )

    assert response.status_code == 401


async def test_users_see_only_their_own_keys(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
) -> None:
    await client.post(
        "/v1/api-keys", json={"name": "alice-1"}, headers=auth_headers
    )

    login = await client.post(
        "/v1/auth/token",
        data={"username": "bob", "password": "another-correct-password"},
    )
    bob_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    await client.post(
        "/v1/api-keys", json={"name": "bob-1"}, headers=bob_headers
    )

    alice_list = await client.get("/v1/api-keys", headers=auth_headers)
    bob_list = await client.get("/v1/api-keys", headers=bob_headers)

    assert [item["name"] for item in alice_list.json()["items"]] == ["alice-1"]
    assert [item["name"] for item in bob_list.json()["items"]] == ["bob-1"]


async def test_touch_throttles_last_used_writes(
    db_session_factory: async_sessionmaker[AsyncSession],
    entities: SeededEntities,
) -> None:
    """One write per granularity window, not one per request."""
    async with db_session_factory() as session:
        minted = await mint_api_key(session, user_id=entities.user.id, name="t")
        await session.commit()

    async with db_session_factory() as session:
        assert await authenticate_api_key(session, minted.plaintext) is not None
        record = await session.get(ApiKey, minted.record.id)
        assert record is not None
        first_write = record.last_used_at
        assert first_write is not None

    # Backdate past the window: the next authentication must move it.
    async with db_session_factory() as session:
        record = await session.get(ApiKey, minted.record.id)
        assert record is not None
        record.last_used_at = datetime.now(UTC).replace(year=2020)
        await session.commit()

    async with db_session_factory() as session:
        assert await authenticate_api_key(session, minted.plaintext) is not None
        record = await session.get(ApiKey, minted.record.id)
        assert record is not None
        assert record.last_used_at is not None
        # SQLite hands back naive datetimes; normalise before comparing.
        seen = record.last_used_at
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=UTC)
        assert seen > datetime(2021, 1, 1, tzinfo=UTC)


async def test_touch_does_not_write_inside_the_window(
    db_session_factory: async_sessionmaker[AsyncSession],
    entities: SeededEntities,
) -> None:
    async with db_session_factory() as session:
        minted = await mint_api_key(session, user_id=entities.user.id, name="t")
        await session.commit()

    async with db_session_factory() as session:
        assert await authenticate_api_key(session, minted.plaintext) is not None

    async with db_session_factory() as session:
        record = await session.get(ApiKey, minted.record.id)
        assert record is not None
        fresh = record.last_used_at
        assert fresh is not None
        assert await authenticate_api_key(session, minted.plaintext) is not None
        await session.refresh(record)
        assert record.last_used_at == fresh


async def test_lookup_is_by_hash_only(
    db_session_factory: async_sessionmaker[AsyncSession],
    entities: SeededEntities,
) -> None:
    """Knowing a prefix must not be enough to authenticate."""
    async with db_session_factory() as session:
        minted = await mint_api_key(session, user_id=entities.user.id, name="p")
        await session.commit()

    prefix_only = minted.plaintext[:11]
    async with db_session_factory() as session:
        found = await session.scalar(
            select(ApiKey).where(ApiKey.key_hash == hash_api_key(prefix_only))
        )
        assert found is None
        assert await authenticate_api_key(session, prefix_only) is None


async def test_user_model_is_returned_for_valid_key(
    db_session_factory: async_sessionmaker[AsyncSession],
    entities: SeededEntities,
) -> None:
    async with db_session_factory() as session:
        minted = await mint_api_key(session, user_id=entities.user.id, name="u")
        await session.commit()

        user = await authenticate_api_key(session, minted.plaintext)
        assert isinstance(user, User)


async def test_get_bearer_user_accepts_an_api_key(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    async with db_session_factory() as session:
        key = await mint_api_key(session, user_id=entities.user.id, name="agent")
        await session.commit()
        user = await get_bearer_user(
            f"Bearer {key.plaintext}", session=session, settings=settings
        )
    assert user.username == "alice"


async def test_get_bearer_user_accepts_a_jwt(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    token = auth_headers["Authorization"].split(" ", 1)[1]
    async with db_session_factory() as session:
        user = await get_bearer_user(f"Bearer {token}", session=session, settings=settings)
    assert user.username == "alice"


async def test_get_bearer_user_rejects_a_revoked_key(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    created = await client.post(
        "/v1/api-keys", json={"name": "agent"}, headers=auth_headers
    )
    async with db_session_factory() as session:
        record = await session.scalar(
            select(ApiKey).where(ApiKey.key_hash == hash_api_key(created.json()["key"]))
        )
        assert record is not None
        record.revoked_at = datetime.now(UTC)
        await session.commit()

        with pytest.raises(AppError) as excinfo:
            await get_bearer_user(
                f"Bearer {created.json()['key']}", session=session, settings=settings
            )
    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "invalid_credentials"


async def test_get_bearer_user_rejects_a_malformed_header(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    async with db_session_factory() as session:
        for header in (None, "", "Basic abc", "Bearer", "rk_short"):
            with pytest.raises(AppError) as excinfo:
                await get_bearer_user(header, session=session, settings=settings)
            assert excinfo.value.status_code == 401
            assert excinfo.value.code == "invalid_credentials"


async def test_get_bearer_user_rejects_a_token_the_wrong_way_round(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """A JWT is not an API key and vice versa.

    The dispatch is by shape, so a plausible-but-unknown ``rk_`` value has to
    fail as an unknown key rather than being decoded as a JWT and 500ing.
    """
    forged = "rk_" + "a" * 43
    async with db_session_factory() as session:
        with pytest.raises(AppError) as excinfo:
            await get_bearer_user(f"Bearer {forged}", session=session, settings=settings)
    assert excinfo.value.status_code == 401
