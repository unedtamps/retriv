import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID, uuid4

import jwt
from anyio import to_thread
from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from jwt import InvalidTokenError
from pwdlib import PasswordHash
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.db.models import User
from app.db.session import get_session

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/auth/token")
password_hash = PasswordHash.recommended()
DUMMY_PASSWORD_HASH = password_hash.hash("not-a-real-password")
PASSWORD_VERIFY_SLOTS = asyncio.Semaphore(2)


class AccessTokenClaims(BaseModel):
    sub: UUID
    role: str
    auth_version: int
    type: str
    iss: str
    aud: str
    iat: datetime
    nbf: datetime
    exp: datetime
    jti: UUID


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, encoded_hash: str) -> bool:
    return password_hash.verify(password, encoded_hash)


def verify_and_update_password(
    password: str, encoded_hash: str
) -> tuple[bool, str | None]:
    return password_hash.verify_and_update(password, encoded_hash)


def create_access_token(user: User, settings: Settings) -> tuple[str, int]:
    now = datetime.now(UTC)
    expires_delta = timedelta(minutes=settings.access_token_minutes)
    payload: dict[str, Any] = {
        "sub": str(user.id),
        "role": user.role,
        "auth_version": user.auth_version,
        "type": "access",
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "nbf": now,
        "exp": now + expires_delta,
        "jti": str(uuid4()),
    }
    token = jwt.encode(
        payload,
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )
    return token, int(expires_delta.total_seconds())


def decode_access_token(token: str, settings: Settings) -> AccessTokenClaims:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={
                "require": [
                    "sub",
                    "role",
                    "auth_version",
                    "type",
                    "iss",
                    "aud",
                    "iat",
                    "nbf",
                    "exp",
                    "jti",
                ]
            },
        )
        claims = AccessTokenClaims.model_validate(payload)
    except (InvalidTokenError, ValidationError, ValueError) as exc:
        raise authentication_error() from exc

    if claims.type != "access":
        raise authentication_error()
    return claims


def authentication_error() -> AppError:
    return AppError(
        status_code=401,
        code="invalid_credentials",
        title="Authentication failed",
        detail="The supplied credentials could not be validated.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def authenticate_user(
    session: AsyncSession, username: str, password: str
) -> User:
    normalized_username = username.strip().lower()
    user = await session.scalar(
        select(User).where(User.username == normalized_username)
    )

    encoded_hash = user.password_hash if user is not None else DUMMY_PASSWORD_HASH
    async with PASSWORD_VERIFY_SLOTS:
        password_valid, updated_hash = await to_thread.run_sync(
            verify_and_update_password, password, encoded_hash
        )
    if user is None or not password_valid or not user.is_active:
        raise authentication_error()
    if updated_hash is not None:
        user.password_hash = updated_hash
    return user


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> User:
    claims = decode_access_token(token, settings)
    user = await session.get(User, claims.sub)
    if (
        user is None
        or not user.is_active
        or user.auth_version != claims.auth_version
    ):
        raise authentication_error()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
