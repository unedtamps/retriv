import base64
from datetime import datetime
from typing import Literal
from uuid import UUID

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from jwt import InvalidTokenError
from pydantic import BaseModel, Field, ValidationError, field_validator

from agent.config import Settings

SCOPE_AUDIENCE = "retriv-agent-answer"
SCOPE_TYPE = "context_search"
SCOPE_ALGORITHM = "RS256"
MAX_SCOPE_WORKSPACES = 1000
MAX_SCOPE_TTL_SECONDS = 900


class ContextScope(BaseModel):
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


class InvalidContextScope(ValueError):
    """Raised when a Retriv-signed scope is invalid or expired."""


def verify_context_scope(token: str, settings: Settings) -> ContextScope:
    try:
        public_key = load_pem_public_key(
            base64.b64decode(settings.scope_public_key_b64.get_secret_value(), validate=True)
        )
        if not isinstance(public_key, RSAPublicKey):
            raise ValueError("The context scope verification key must be RSA")
        payload = jwt.decode(
            token,
            public_key,
            algorithms=[SCOPE_ALGORITHM],
            audience=SCOPE_AUDIENCE,
            issuer=settings.scope_issuer,
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
        claims = ContextScope.model_validate(payload)
        if (claims.exp - claims.iat).total_seconds() > MAX_SCOPE_TTL_SECONDS:
            raise ValueError("The delegated context scope exceeds the allowed lifetime")
        return claims
    except (InvalidTokenError, ValidationError, ValueError, TypeError) as exc:
        raise InvalidContextScope("The delegated context scope is invalid or expired") from exc
