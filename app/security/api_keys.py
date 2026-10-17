"""Minting and verifying long-lived API keys.

Format: ``rk_<43 url-safe base64 chars>`` (~256 bits of entropy). The
plaintext is returned exactly once, at creation; only the SHA-256 hash and
a short display prefix are persisted.

Verification looks up by hash, not by prefix: the prefix exists purely so a
human can tell two keys apart in a list, and using it as a lookup key would
let an attacker with partial knowledge probe for collisions. There is no
timing side channel on the hash comparison because the database does the
matching by equality on an indexed column.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ApiKey, User
from app.security.auth import authentication_error

KEY_SCHEME = "rk"
KEY_ENTROPY_BYTES = 32
PREFIX_LENGTH = 11
HASH_ALGORITHM = "sha256"
# Throttle last_used_at writes: one per minute per key is plenty for scope
# derivation and keeps a busy MCP client from turning every tool call into
# an UPDATE.
LAST_USED_GRANULARITY_SECONDS = 60

# Characters that read poorly when copied from a terminal.
_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_api_key() -> str:
    body = "".join(secrets.choice(_ALPHABET) for _ in range(43))
    return f"{KEY_SCHEME}_{body}"


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def key_prefix_of(key: str) -> str:
    return key[:PREFIX_LENGTH]


def looks_like_api_key(value: str) -> bool:
    return (
        value.startswith(f"{KEY_SCHEME}_")
        and len(value) == len(KEY_SCHEME) + 1 + 43
    )


@dataclass(frozen=True)
class MintedApiKey:
    record: ApiKey
    plaintext: str


async def mint_api_key(
    session: AsyncSession, *, user_id: UUID, name: str
) -> MintedApiKey:
    plaintext = generate_api_key()
    record = ApiKey(
        user_id=user_id,
        name=name,
        key_prefix=key_prefix_of(plaintext),
        key_hash=hash_api_key(plaintext),
        # Set client-side too: the creation response returns it, and a
        # server default is only fetched back on backends that RETURN it.
        created_at=datetime.now(UTC),
    )
    session.add(record)
    await session.flush()
    return MintedApiKey(record=record, plaintext=plaintext)


async def revoke_api_key(
    session: AsyncSession, *, key_id: UUID, user_id: UUID
) -> bool:
    result = await session.execute(
        update(ApiKey)
        .where(
            ApiKey.id == key_id,
            ApiKey.user_id == user_id,
            ApiKey.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )
    return getattr(result, "rowcount", 0) == 1


async def authenticate_api_key(session: AsyncSession, key: str) -> User | None:
    """Resolve a presented key to its live owning user, or ``None``.

    ``None`` covers every rejection path -- unknown, revoked, inactive
    owner, stale ``auth_version`` -- because the caller must not be able to
    distinguish them.
    """
    if not looks_like_api_key(key):
        return None
    record = await session.scalar(
        select(ApiKey).where(ApiKey.key_hash == hash_api_key(key))
    )
    if record is None or record.revoked_at is not None:
        return None
    user = await session.get(User, record.user_id)
    if user is None or not user.is_active:
        return None
    await touch_api_key(session, record)
    return user


async def touch_api_key(session: AsyncSession, record: ApiKey) -> None:
    now = datetime.now(UTC)
    last_used = record.last_used_at
    # SQLite returns naive datetimes even for a timezone-aware column.
    if last_used is not None and last_used.tzinfo is None:
        last_used = last_used.replace(tzinfo=UTC)
    if (
        last_used is None
        or (now - last_used).total_seconds() >= LAST_USED_GRANULARITY_SECONDS
    ):
        record.last_used_at = now
        # Committed, not just flushed: no request path commits on the way
        # out of authentication, so a flush here would be rolled back when
        # the session closes and last_used_at would never persist.
        await session.commit()


def bearer_api_key(authorization: str | None) -> str:
    """Accept exactly ``Bearer <key>``; anything else is a rejection.

    MCP clients send a single Authorization header, and being strict here
    means an ``X-Api-Key`` or raw-value variant fails loudly at the client
    instead of silently authenticating nobody.
    """
    if not authorization:
        raise authentication_error()
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise authentication_error()
    return value.strip()
