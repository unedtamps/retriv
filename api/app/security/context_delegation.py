import base64
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from jwt import InvalidTokenError
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.core.config import Settings
from app.core.errors import AppError
from app.db.models import User

CONTEXT_SCOPE_AUDIENCE = "retriv-agent-answer"
CONTEXT_SCOPE_TYPE = "context_search"
MAX_SCOPE_WORKSPACES = 1000
SCOPE_ALGORITHM = "RS256"


class ContextDelegationClaims(BaseModel):
    sub: UUID
    auth_version: int
    workspace_ids: list[UUID] = Field(min_length=1, max_length=MAX_SCOPE_WORKSPACES)
    type: Literal["context_search"]
    iss: str
    aud: str
    iat: datetime
    nbf: datetime
    exp: datetime
    jti: UUID

    @field_validator("workspace_ids")
    @classmethod
    def require_unique_workspace_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("Workspace scope must not contain duplicate IDs")
        return value


def create_context_delegation(
    user: User, workspace_ids: list[UUID], settings: Settings
) -> str:
    if not workspace_ids or len(workspace_ids) > MAX_SCOPE_WORKSPACES:
        raise ValueError("Workspace scope must contain between 1 and 1000 IDs")
    now = datetime.now(UTC)
    payload = {
        "sub": str(user.id),
        "auth_version": user.auth_version,
        "workspace_ids": [str(workspace_id) for workspace_id in workspace_ids],
        "type": CONTEXT_SCOPE_TYPE,
        "iss": settings.jwt_issuer,
        "aud": CONTEXT_SCOPE_AUDIENCE,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(seconds=settings.agent_scope_ttl_seconds),
        "jti": str(uuid4()),
    }
    try:
        private_key = base64.b64decode(
            settings.agent_scope_private_key_b64.get_secret_value(), validate=True
        )
        return jwt.encode(payload, private_key, algorithm=SCOPE_ALGORITHM)
    except (ValueError, TypeError) as exc:
        raise ValueError("The agent scope signing key is invalid") from exc


def decode_context_delegation(token: str, settings: Settings) -> ContextDelegationClaims:
    try:
        private_key = load_pem_private_key(
            base64.b64decode(
                settings.agent_scope_private_key_b64.get_secret_value(), validate=True
            ),
            password=None,
        )
        if not isinstance(private_key, RSAPrivateKey):
            raise ValueError("The agent scope signing key must be an RSA private key")
        payload = jwt.decode(
            token,
            private_key.public_key(),
            algorithms=[SCOPE_ALGORITHM],
            audience=CONTEXT_SCOPE_AUDIENCE,
            issuer=settings.jwt_issuer,
            options={
                "require": [
                    "sub",
                    "auth_version",
                    "workspace_ids",
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
        claims = ContextDelegationClaims.model_validate(payload)
    except (InvalidTokenError, ValidationError, ValueError, TypeError) as exc:
        raise invalid_delegation_error() from exc
    return claims


def invalid_delegation_error() -> AppError:
    return AppError(
        status_code=401,
        code="invalid_context_delegation",
        title="Context delegation is invalid",
        detail="The delegated context scope is invalid or expired.",
        headers={"WWW-Authenticate": "Bearer"},
    )
