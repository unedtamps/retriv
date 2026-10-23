"""Create or update the first platform administrator from the environment.

There is no self-registration and no manifest to import: a fresh deployment
needs exactly one account that can create the rest. The API creates it on
startup from ``RETRIV_BOOTSTRAP_ADMIN_USERNAME`` and
``RETRIV_BOOTSTRAP_ADMIN_PASSWORD``.

The environment is the source of truth for this one account. If the row does
not exist it is created; if the password in the environment no longer matches
the stored hash, the hash is replaced and ``auth_version`` is bumped so every
session issued under the old password is signed out. The role and active flag
are left alone, so an operator can still disable the account without the next
restart undoing it.
"""

import logging
from typing import Any

from anyio import to_thread
from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import User
from app.security.auth import hash_password, verify_password

logger = logging.getLogger(__name__)


def _user_insert(session: AsyncSession, values: dict[str, Any]) -> Any:
    """The insert that leaves an existing username alone.

    Two API replicas starting at the same moment both try to create the
    same row. The unique index on ``username`` is what decides, and
    ``ON CONFLICT DO NOTHING`` turns the loser into a no-op instead of a
    startup crash. PostgreSQL and SQLite spell this the same way through
    their own dialect helpers; anything else is not a database this
    application supports.
    """
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        return sqlite_insert(User).values(**values).on_conflict_do_nothing(
            index_elements=["username"]
        )
    if dialect == "postgresql":
        return postgresql_insert(User).values(**values).on_conflict_do_nothing(
            index_elements=["username"]
        )
    return insert(User).values(**values)


async def ensure_bootstrap_admin(
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Make the bootstrap administrator match the environment.

    A no-op when the pair is unset. Everything that touches Argon2 runs in
    a worker thread, because hashing and verifying are deliberately slow
    and this runs on the startup path.
    """
    username = settings.bootstrap_admin_username
    password = settings.bootstrap_admin_password.get_secret_value()
    if not username or not password:
        return

    async with sessions() as session, session.begin():
        user = await session.scalar(
            select(User).where(User.username == username).with_for_update()
        )
        if user is None:
            password_hash = await to_thread.run_sync(hash_password, password)
            statement = _user_insert(
                session,
                {
                    "username": username,
                    "password_hash": password_hash,
                    "role": "admin",
                    "is_active": True,
                },
            )
            result = await session.execute(statement)
            if int(getattr(result, "rowcount", 0) or 0) == 1:
                logger.info("Created bootstrap administrator %r", username)
                return
            # Another replica created it between the read and the insert.
            # Read the row it wrote and carry on to the password check.
            user = await session.scalar(
                select(User).where(User.username == username).with_for_update()
            )
            if user is None:
                return

        if user.role != "admin":
            logger.warning(
                "Bootstrap administrator %r already exists with role %r; "
                "the password is kept in sync but the role is left alone",
                username,
                user.role,
            )

        if await to_thread.run_sync(verify_password, password, user.password_hash):
            return

        user.password_hash = await to_thread.run_sync(hash_password, password)
        user.auth_version += 1
        logger.info("Updated bootstrap administrator password for %r", username)
