import asyncio
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import User, Workspace
from app.infrastructure.storage import (
    ObjectNotFoundError,
    ObjectStorage,
    S3ObjectStorage,
    StorageError,
)
from app.security.auth import hash_password
from provisioning.models import ProvisioningRun

ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9:_-]{1,63}$")
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ProvisionerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="PROVISIONER_",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = ""
    s3_endpoint_url: str = ""
    s3_access_key: str = ""
    s3_secret_key: SecretStr = SecretStr("")
    s3_region: str = "us-east-1"
    manifest_bucket: str = "migration-manifests"
    raw_document_bucket: str = ""
    manifest_prefix: str = "bootstrap/"
    manifest_uri: str = ""
    environment: Literal["local", "test", "production"] = "local"
    max_manifest_bytes: int = Field(default=1024 * 1024, gt=0, le=10 * 1024 * 1024)

    @model_validator(mode="after")
    def validate_required_values(self) -> "ProvisionerSettings":
        if not self.database_url.strip():
            raise ValueError("PROVISIONER_DATABASE_URL is required")
        if not self.s3_endpoint_url.strip():
            raise ValueError("PROVISIONER_S3_ENDPOINT_URL is required")
        if not self.s3_access_key.strip() or not self.s3_secret_key.get_secret_value():
            raise ValueError("Provisioner object-store credentials are required")
        if not self.raw_document_bucket.strip():
            raise ValueError("PROVISIONER_RAW_DOCUMENT_BUCKET is required")
        if self.raw_document_bucket == self.manifest_bucket:
            raise ValueError("Manifest and raw-document buckets must be different")
        if not self.manifest_prefix or self.manifest_prefix.startswith("/"):
            raise ValueError("PROVISIONER_MANIFEST_PREFIX must be a relative prefix")
        if ".." in self.manifest_prefix or not self.manifest_prefix.endswith("/"):
            raise ValueError(
                "PROVISIONER_MANIFEST_PREFIX must be a safe directory prefix"
            )
        if not self.manifest_uri.strip():
            raise ValueError("PROVISIONER_MANIFEST_URI is required")
        self.manifest_uri = self.manifest_uri.strip()
        if self.environment == "production" and not self.s3_endpoint_url.startswith(
            "https://"
        ):
            raise ValueError("Provisioner object storage must use HTTPS in production")
        return self


class UserManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: SecretStr
    role: str = "user"
    is_active: bool = True

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized or len(normalized) > 255:
            raise ValueError("Username must contain between 1 and 255 characters")
        return normalized

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: SecretStr) -> SecretStr:
        password = value.get_secret_value()
        password_bytes = password.encode("utf-8")
        if len(password_bytes) < 12:
            raise ValueError("Password must contain at least 12 bytes")
        if len(password_bytes) > 1024:
            raise ValueError("Password cannot exceed 1024 bytes")
        return value

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not ROLE_PATTERN.fullmatch(normalized):
            raise ValueError("Role must be a lowercase identifier")
        return normalized


class WorkspaceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str
    description: str | None = None

    @field_validator("slug")
    @classmethod
    def normalize_slug(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) > 100 or not SLUG_PATTERN.fullmatch(normalized):
            raise ValueError(
                "Slug must contain lowercase words separated by single hyphens"
            )
        return normalized

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 255:
            raise ValueError("Name must contain between 1 and 255 characters")
        return normalized

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if len(normalized) > 1000:
            raise ValueError("Description cannot exceed 1000 characters")
        return normalized or None


class ProvisioningManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    migration_id: str = Field(min_length=1, max_length=255)
    users: list[UserManifest] = Field(default_factory=list, max_length=1000)
    workspaces: list[WorkspaceManifest] = Field(default_factory=list, max_length=1000)

    @field_validator("migration_id")
    @classmethod
    def normalize_migration_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("migration_id cannot be empty")
        return normalized

    @model_validator(mode="after")
    def validate_unique_keys(self) -> "ProvisioningManifest":
        usernames = [user.username for user in self.users]
        if len(usernames) != len(set(usernames)):
            raise ValueError("Manifest contains duplicate usernames")
        slugs = [workspace.slug for workspace in self.workspaces]
        if len(slugs) != len(set(slugs)):
            raise ValueError("Manifest contains duplicate workspace slugs")
        if not self.users and not self.workspaces:
            raise ValueError("Manifest must contain at least one user or workspace")
        return self


class ProvisioningError(Exception):
    """A safe, operator-facing provisioning failure."""


class ManifestConflictError(ProvisioningError):
    """The migration ID was previously used with a different manifest."""


class ManifestInProgressError(ProvisioningError):
    """Another provisioning transaction is using the migration ID."""


class ProvisioningResult(BaseModel):
    migration_id: str
    manifest_sha256: str
    source_uri: str
    already_applied: bool = False
    users_created: int = 0
    users_updated: int = 0
    workspaces_created: int = 0
    workspaces_updated: int = 0


def parse_manifest(payload: bytes) -> tuple[ProvisioningManifest, str]:
    digest = hashlib.sha256(payload).hexdigest()
    try:
        manifest = ProvisioningManifest.model_validate_json(payload)
    except (ValidationError, ValueError):
        raise ProvisioningError("Manifest JSON failed validation") from None
    return manifest, digest


def parse_manifest_uri(
    uri: str, expected_bucket: str, expected_prefix: str
) -> tuple[str, str]:
    parsed = urlsplit(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise ProvisioningError("Manifest URI must use the s3://bucket/key format")
    if parsed.query or parsed.fragment:
        raise ProvisioningError("Manifest URI cannot contain a query or fragment")
    bucket = parsed.netloc
    key = unquote(parsed.path.lstrip("/"))
    if bucket != expected_bucket:
        raise ProvisioningError(
            "Manifest URI bucket does not match the configured bucket"
        )
    if not key.startswith(expected_prefix) or key == expected_prefix:
        raise ProvisioningError(
            "Manifest URI is outside the configured manifest prefix"
        )
    return bucket, key


def read_manifest_object(
    storage: ObjectStorage, *, uri: str, bucket: str, prefix: str, max_bytes: int
) -> bytes:
    object_bucket, object_key = parse_manifest_uri(uri, bucket, prefix)
    try:
        stream = storage.open_object(bucket=object_bucket, key=object_key)
    except (ObjectNotFoundError, StorageError):
        raise ProvisioningError("Manifest object could not be read") from None

    payload = bytearray()
    try:
        while True:
            chunk = stream.read(min(64 * 1024, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > max_bytes:
                raise ProvisioningError(
                    "Manifest object exceeds the configured size limit"
                )
    finally:
        stream.close()
    return bytes(payload)


def _advisory_lock_key(migration_id: str) -> int:
    digest = hashlib.sha256(migration_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def _lock_migration(session: AsyncSession, migration_id: str) -> None:
    bind = session.sync_session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _advisory_lock_key(migration_id)},
        )


async def apply_manifest(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    manifest_payload: bytes,
    source_uri: str,
) -> ProvisioningResult:
    manifest, manifest_sha256 = parse_manifest(manifest_payload)
    async with session_factory() as session, session.begin():
        await _lock_migration(session, manifest.migration_id)
        existing = await session.scalar(
            select(ProvisioningRun)
            .where(ProvisioningRun.migration_id == manifest.migration_id)
            .with_for_update()
        )
        if existing is not None:
            if existing.manifest_sha256 != manifest_sha256:
                raise ManifestConflictError(
                    "migration_id was already used with a different manifest"
                )
            if existing.status != "completed":
                raise ManifestInProgressError("migration_id is already in progress")
            result = ProvisioningResult.model_validate(existing.result)
            result.already_applied = True
            return result

        run = ProvisioningRun(
            migration_id=manifest.migration_id,
            manifest_sha256=manifest_sha256,
            source_uri=source_uri,
            status="running",
        )
        session.add(run)
        await session.flush()

        result = ProvisioningResult(
            migration_id=manifest.migration_id,
            manifest_sha256=manifest_sha256,
            source_uri=source_uri,
        )
        for workspace_manifest in manifest.workspaces:
            workspace = await session.scalar(
                select(Workspace)
                .where(Workspace.slug == workspace_manifest.slug)
                .with_for_update()
            )
            if workspace is None:
                workspace = Workspace(
                    slug=workspace_manifest.slug,
                    name=workspace_manifest.name,
                    description=workspace_manifest.description,
                )
                session.add(workspace)
                result.workspaces_created += 1
            else:
                workspace.name = workspace_manifest.name
                workspace.description = workspace_manifest.description
                result.workspaces_updated += 1

        for user_manifest in manifest.users:
            user = await session.scalar(
                select(User)
                .where(User.username == user_manifest.username)
                .with_for_update()
            )
            password_hash = hash_password(user_manifest.password.get_secret_value())
            if user is None:
                user = User(
                    username=user_manifest.username,
                    password_hash=password_hash,
                    role=user_manifest.role,
                    is_active=user_manifest.is_active,
                )
                session.add(user)
                result.users_created += 1
            else:
                user.password_hash = password_hash
                user.role = user_manifest.role
                user.is_active = user_manifest.is_active
                # Credentials or permissions changed: invalidate sessions issued
                # before this update so stolen tokens cannot outlive a rotation.
                user.auth_version += 1
                result.users_updated += 1

        result_payload = result.model_dump(mode="json")
        run.status = "completed"
        run.result = result_payload
        run.completed_at = datetime.now(UTC)
        await session.flush()
        return result


async def run_provisioning(settings: ProvisionerSettings) -> ProvisioningResult:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    storage = S3ObjectStorage(
        endpoint_url=settings.s3_endpoint_url,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key.get_secret_value(),
        region=settings.s3_region,
    )
    try:
        manifest_payload = read_manifest_object(
            storage,
            uri=settings.manifest_uri,
            bucket=settings.manifest_bucket,
            prefix=settings.manifest_prefix,
            max_bytes=settings.max_manifest_bytes,
        )
        return await apply_manifest(
            session_factory,
            manifest_payload=manifest_payload,
            source_uri=settings.manifest_uri,
        )
    finally:
        await engine.dispose()


def main() -> None:
    try:
        settings = ProvisionerSettings()
        result = asyncio.run(run_provisioning(settings))
    except (ValidationError, ProvisioningError) as exc:
        print(f"Provisioning failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception:
        print(
            "Provisioning failed due to an unexpected infrastructure error",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    print(json.dumps(result.model_dump(mode="json"), sort_keys=True))


if __name__ == "__main__":
    main()
