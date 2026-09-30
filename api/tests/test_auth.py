from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import User
from app.security.auth import create_access_token, decode_access_token
from tests.conftest import SeededEntities


async def test_login_returns_valid_role_bearing_jwt(
    client: httpx.AsyncClient,
    entities: SeededEntities,
    settings: Settings,
) -> None:
    response = await client.post(
        "/v1/auth/token",
        data={"username": "alice", "password": "correct-horse-battery-staple"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    assert response.headers["cache-control"] == "no-store"
    claims = jwt.decode(
        body["access_token"],
        settings.jwt_secret.get_secret_value(),
        algorithms=[settings.jwt_algorithm],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
    )
    assert claims["sub"] == str(entities.user.id)
    assert claims["role"] == "user"
    assert claims["type"] == "access"
    assert datetime.fromtimestamp(claims["exp"], UTC) > datetime.now(UTC)


async def test_login_failure_is_generic(
    client: httpx.AsyncClient, entities: SeededEntities
) -> None:
    del entities
    response = await client.post(
        "/v1/auth/token",
        data={"username": "alice", "password": "wrong-password"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_protected_endpoint_rejects_missing_token(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/workspaces")

    assert response.status_code == 401


async def test_cors_preflight_allows_configured_dashboard_origin(
    client: httpx.AsyncClient,
) -> None:
    response = await client.options(
        "/v1/workspaces",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


async def test_cors_preflight_rejects_unconfigured_origin(
    client: httpx.AsyncClient,
) -> None:
    response = await client.options(
        "/v1/workspaces",
        headers={
            "Origin": "https://untrusted.example.test",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Authorization",
        },
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


async def test_missing_token_does_not_leak_workspace_existence(
    client: httpx.AsyncClient, entities: SeededEntities
) -> None:
    existing = await client.get(f"/v1/workspaces/{entities.finance.id}/documents")
    missing = await client.get("/v1/workspaces/00000000-0000-0000-0000-000000000001/documents")

    assert existing.status_code == 401
    assert missing.status_code == 401


async def test_login_request_body_has_small_preparse_limit(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/v1/auth/token",
        data={"username": "x" * 20_000, "password": "irrelevant"},
    )

    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"


async def test_failed_logins_are_rate_limited(client: httpx.AsyncClient) -> None:
    for _ in range(5):
        response = await client.post(
            "/v1/auth/token",
            data={"username": "rate-limit-target", "password": "wrong-password"},
        )
        assert response.status_code == 401

    throttled = await client.post(
        "/v1/auth/token",
        data={"username": "rate-limit-target", "password": "wrong-password"},
    )

    assert throttled.status_code == 429
    assert throttled.json()["code"] == "login_rate_limited"
    assert int(throttled.headers["retry-after"]) > 0


async def test_deactivated_user_cannot_reuse_existing_token(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    entities: SeededEntities,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as session:
        user = await session.get(User, entities.user.id)
        assert user is not None
        user.is_active = False
        await session.commit()

    response = await client.get("/v1/workspaces", headers=auth_headers)

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"


@pytest.mark.parametrize("invalid_kind", ["expired", "audience", "type", "algorithm"])
def test_decode_rejects_invalid_jwt_variants(
    invalid_kind: str,
    entities: SeededEntities,
    settings: Settings,
) -> None:
    token, _ = create_access_token(entities.user, settings)
    payload = jwt.decode(token, options={"verify_signature": False})
    algorithm = settings.jwt_algorithm
    if invalid_kind == "expired":
        payload["exp"] = datetime.now(UTC) - timedelta(seconds=1)
    elif invalid_kind == "audience":
        payload["aud"] = "wrong-audience"
    elif invalid_kind == "type":
        payload["type"] = "refresh"
    else:
        algorithm = "HS512"
    invalid_token = jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=algorithm,
    )

    with pytest.raises(AppError) as error:
        decode_access_token(invalid_token, settings)
    assert error.value.status_code == 401
