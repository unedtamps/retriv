import asyncio
import hashlib
import json
import re
import sys
import traceback
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal
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
from sqlalchemy.exc import SQLAlchemyError
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

# How many problems a single report will carry. A manifest can be wrong in
# a hundred ways at once; listing all of them buries the operator.
MAX_REPORTED_PROBLEMS = 5
MANIFEST_HINT = (
    "fix the fields above and re-upload the manifest to the URI in "
    "PROVISIONER_MANIFEST_URI"
)
SETTINGS_HINT = "set the variables above in .env; the example lists all of them"


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
        # Collect everything wrong rather than raising on the first miss.
        # A first run with nothing configured used to take six runs to
        # discover six missing variables, one message at a time.
        problems: list[str] = []
        if not self.database_url.strip():
            problems.append("PROVISIONER_DATABASE_URL is required")
        if not self.s3_endpoint_url.strip():
            problems.append("PROVISIONER_S3_ENDPOINT_URL is required")
        if not self.s3_access_key.strip() or not self.s3_secret_key.get_secret_value():
            problems.append(
                "PROVISIONER_S3_ACCESS_KEY and PROVISIONER_S3_SECRET_KEY are required"
            )
        if not self.raw_document_bucket.strip():
            problems.append("PROVISIONER_RAW_DOCUMENT_BUCKET is required")
        elif self.raw_document_bucket == self.manifest_bucket:
            problems.append(
                "PROVISIONER_RAW_DOCUMENT_BUCKET must differ from "
                "PROVISIONER_MANIFEST_BUCKET"
            )
        if not self.manifest_prefix or self.manifest_prefix.startswith("/"):
            problems.append("PROVISIONER_MANIFEST_PREFIX must be a relative prefix")
        elif ".." in self.manifest_prefix or not self.manifest_prefix.endswith("/"):
            problems.append(
                "PROVISIONER_MANIFEST_PREFIX must be a safe directory prefix ending in /"
            )
        if not self.manifest_uri.strip():
            problems.append("PROVISIONER_MANIFEST_URI is required")
        if self.environment == "production" and self.s3_endpoint_url.strip() and not (
            self.s3_endpoint_url.startswith("https://")
        ):
            problems.append(
                "PROVISIONER_S3_ENDPOINT_URL must use https in production"
            )
        if problems:
            raise ValueError("; ".join(problems))
        self.manifest_uri = self.manifest_uri.strip()
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
    """A safe, operator-facing provisioning failure.

    ``hint`` is part of the contract, not decoration. This job is run by
    hand, and a message an operator cannot act on is worth about the same
    as no message: "the manifest could not be read" is equally true
    whether the file was never uploaded, the reader's policy excludes the
    prefix, the secret does not match, or MinIO is down.
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint

    def render(self) -> str:
        return render_failure(self)


class ManifestValidationError(ProvisioningError):
    """The manifest is not a valid manifest.

    Carries the individual problems, so somebody editing JSON by hand is
    told which field is wrong instead of being told that something,
    somewhere, is wrong.
    """

    def __init__(self, problems: Sequence[str], *, hint: str = MANIFEST_HINT) -> None:
        super().__init__("the manifest failed validation", hint=hint)
        self.problems: tuple[str, ...] = tuple(problems)


class ManifestReadError(ProvisioningError):
    """The manifest object could not be fetched from object storage."""

    def __init__(self, message: str, *, s3_code: str | None, hint: str) -> None:
        super().__init__(message, hint=hint)
        self.s3_code = s3_code


class ManifestConflictError(ProvisioningError):
    """The migration ID was previously used with a different manifest."""

    def __init__(self, migration_id: str) -> None:
        super().__init__(
            f"migration_id '{migration_id}' was already used with a different manifest",
            hint=(
                "an applied manifest is immutable; give the new content a new "
                "migration_id, or restore the original bytes"
            ),
        )
        self.migration_id = migration_id


class ManifestInProgressError(ProvisioningError):
    """Another provisioning transaction is using the migration ID."""

    def __init__(self, migration_id: str) -> None:
        super().__init__(
            f"migration_id '{migration_id}' is already in progress",
            hint="another run holds the advisory lock; wait for it to finish",
        )
        self.migration_id = migration_id


class DatabaseUnreachableError(ProvisioningError):
    """The database could not be opened, so nothing was written."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            "the database could not be reached",
            hint=(
                "check that postgres is up and that PROVISIONER_DATABASE_URL "
                "names the same database as the app"
            ),
        )
        self.detail = detail


def _field_label(location: Sequence[Any]) -> str:
    """Turn a pydantic error location into a readable path.

    List indices are bracketed so ``users[0].password`` reads as a path
    into the document rather than as the name of a key.
    """
    label = ""
    for part in location:
        if isinstance(part, int):
            label += f"[{part}]"
        else:
            label += f".{part}" if label else str(part)
    return label or "<root>"


def describe_validation_error(exc: ValidationError) -> ManifestValidationError:
    """Summarise a pydantic failure into lines an operator can act on.

    Only ``loc`` and ``msg`` are read. ``input``, ``ctx`` and ``url`` are
    not, because they carry the offending value and some of these fields
    are passwords. That constraint is why this cannot just dump
    ``exc.errors()``.
    """
    problems: list[str] = []
    for error in exc.errors()[:MAX_REPORTED_PROBLEMS]:
        message = error["msg"]
        if message.startswith("Value error, "):
            message = message.removeprefix("Value error, ")
        problems.append(f"{_field_label(error['loc'])}\t{message}")
    remaining = len(exc.errors()) - MAX_REPORTED_PROBLEMS
    if remaining > 0:
        problems.append(f"\t...and {remaining} more")
    return ManifestValidationError(problems)


def describe_settings_error(exc: ValidationError) -> ManifestValidationError:
    """Turn a settings failure into the same shape as a manifest one.

    ``validate_required_values`` now raises once with every problem
    joined by semicolons, so one line of pydantic output holds several
    independent problems; they are split back out here to keep the report
    one item per line.
    """
    problems: list[str] = []
    for error in exc.errors():
        message = error["msg"]
        if message.startswith("Value error, "):
            message = message.removeprefix("Value error, ")
        for part in message.split("; "):
            problems.append(f"{_field_label(error['loc'])}\t{part}")
    return ManifestValidationError(problems, hint=SETTINGS_HINT)


def render_failure(exc: ProvisioningError) -> str:
    """Render a failure as the block an operator reads."""
    lines = [f"Provisioning failed: {exc}"]
    if problems := getattr(exc, "problems", None):
        pairs = [problem.split("\t", 1) for problem in problems]
        width = max(len(label) for label, _ in pairs)
        lines.extend(f"  {label.ljust(width)}  {detail}" for label, detail in pairs)
    if (s3_code := getattr(exc, "s3_code", None)) is not None:
        lines.append(f"  s3 code  {s3_code}")
    if (detail := getattr(exc, "detail", None)) is not None:
        lines.append(f"  detail   {detail}")
    if exc.hint:
        lines.append(f"  hint     {exc.hint}")
    return "\n".join(lines)


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
    except ValidationError as exc:
        # Malformed JSON and a schema violation both arrive here: pydantic
        # raises json_invalid with a line and column for the first, which is
        # already more useful than anything this function could add.
        raise describe_validation_error(exc) from None
    return manifest, digest


def parse_manifest_uri(
    uri: str, expected_bucket: str, expected_prefix: str
) -> tuple[str, str]:
    parsed = urlsplit(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise ProvisioningError(
            f"the manifest URI '{uri}' is not in s3://bucket/key form",
            hint="PROVISIONER_MANIFEST_URI should look like s3://<bucket>/<key>",
        )
    if parsed.query or parsed.fragment:
        raise ProvisioningError(
            "the manifest URI contains a query or fragment",
            hint="PROVISIONER_MANIFEST_URI must be a plain s3://bucket/key path",
        )
    bucket = parsed.netloc
    key = unquote(parsed.path.lstrip("/"))
    if bucket != expected_bucket:
        raise ProvisioningError(
            f"the manifest URI names bucket '{bucket}' but "
            f"PROVISIONER_MANIFEST_BUCKET is '{expected_bucket}'",
            hint="point both at the same bucket, or fix the URI",
        )
    if not key.startswith(expected_prefix) or key == expected_prefix:
        raise ProvisioningError(
            f"the manifest key '{key}' is outside PROVISIONER_MANIFEST_PREFIX",
            hint=(
                f"migration-reader may only read under '{expected_prefix}'; "
                "move the manifest there or widen the prefix and re-run "
                "minio-init"
            ),
        )
    return bucket, key


# The S3 codes that each mean a different thing to whoever has to fix it.
_S3_HINTS: dict[str, tuple[str, str]] = {
    "NoSuchKey": (
        "the object does not exist at that path",
        "upload the manifest with the migration-writer identity, or correct "
        "PROVISIONER_MANIFEST_URI",
    ),
    "NoSuchBucket": (
        "the bucket does not exist",
        "minio-init has not run, or the compose project was recreated without it; "
        "run 'make infra'",
    ),
    "AccessDenied": (
        "the read-only identity is not allowed to read that object",
        "migration-reader may only read under PROVISIONER_MANIFEST_PREFIX; check "
        "the prefix matches where you uploaded the file",
    ),
    "SignatureDoesNotMatch": (
        "the secret does not match the access key",
        "PROVISIONER_S3_SECRET_KEY must equal MINIO_MANIFEST_READER_PASSWORD; "
        "nothing interpolates between them",
    ),
    "InvalidAccessKeyId": (
        "the access key is unknown to MinIO",
        "PROVISIONER_S3_ACCESS_KEY must equal MINIO_MANIFEST_READER_USER",
    ),
    "ExpiredToken": (
        "the credentials expired before the request completed",
        "this is a session-token store; check the system clock and re-run",
    ),
}


def _first_line(exc: BaseException) -> str:
    """The single most useful line of a driver or pool error.

    SQLAlchemy renders a refused connection as several lines of chained
    context ending in ``[Errno 111] Connect call failed``. Only the last
    line carries the cause, so the report keeps that and drops the rest.
    """
    text = str(exc).strip()
    if not text:
        return type(exc).__name__
    return text.splitlines()[-1][:200]


def _s3_code_from(exc: BaseException) -> str | None:
    """Recover the S3 error code from the wrapped ClientError.

    The storage layer re-raises with ``from exc``, so botocore's code is
    still reachable on ``__cause__``. Without it every object-store
    failure collapses into one sentence.
    """
    cause: BaseException | None = exc
    for _ in range(5):
        if cause is None:
            return None
        response = getattr(cause, "response", None)
        if isinstance(response, dict):
            code = response.get("Error", {}).get("Code")
            if isinstance(code, str):
                return code
        cause = cause.__cause__
    return None


def read_manifest_object(
    storage: ObjectStorage, *, uri: str, bucket: str, prefix: str, max_bytes: int
) -> bytes:
    object_bucket, object_key = parse_manifest_uri(uri, bucket, prefix)
    try:
        stream = storage.open_object(bucket=object_bucket, key=object_key)
    except (ObjectNotFoundError, StorageError) as exc:
        code = _s3_code_from(exc)
        message, hint = _S3_HINTS.get(
            code or "",
            (
                "the object could not be read from object storage",
                "check that MinIO is up and reachable at PROVISIONER_S3_ENDPOINT_URL",
            ),
        )
        raise ManifestReadError(message, s3_code=code, hint=hint) from None

    payload = bytearray()
    try:
        while True:
            chunk = stream.read(min(64 * 1024, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > max_bytes:
                raise ProvisioningError(
                    f"the manifest exceeds the {max_bytes} byte limit",
                    hint="raise PROVISIONER_MAX_MANIFEST_BYTES, or split the manifest",
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
                raise ManifestConflictError(manifest.migration_id)
            if existing.status != "completed":
                raise ManifestInProgressError(manifest.migration_id)
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
    # The engine is lazy: creating it does not touch the network, so a
    # dead database surfaces here rather than at engine construction.
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
    finally:
        await engine.dispose()

    # The database is only touched here, so an unreachable server is
    # reported as its own failure rather than as a generic crash. Nothing
    # has been written at this point either way.
    #
    # OSError is caught alongside SQLAlchemyError because asyncpg surfaces
    # a refused socket, an unknown host, or a failed TLS handshake as the
    # underlying OSError rather than wrapping it. Catching only the
    # SQLAlchemy type let all three reach the traceback handler, which
    # answered "the database is not running" with sixty lines of pool
    # internals.
    async with session_factory() as probe:
        try:
            await probe.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError) as exc:
            raise DatabaseUnreachableError(_first_line(exc)) from None

    return await apply_manifest(
        session_factory,
        manifest_payload=manifest_payload,
        source_uri=settings.manifest_uri,
    )


def main() -> None:
    try:
        settings = ProvisionerSettings()
    except ValidationError as exc:
        # Settings problems get the same field-by-field treatment as the
        # manifest. They are the most common failure on a first run and
        # they are all the operator's own configuration.
        print(describe_settings_error(exc).render(), file=sys.stderr)
        raise SystemExit(2) from None

    try:
        result = asyncio.run(run_provisioning(settings))
    except ProvisioningError as exc:
        # Exit 2: the operator's input was wrong, or the world was not in
        # the state the job needs. Nothing was written.
        print(exc.render(), file=sys.stderr)
        raise SystemExit(2) from None
    except Exception:
        # Exit 1, and this time the traceback is printed. A hand-run job
        # that fails unexpectedly should hand over the evidence; the old
        # bare handler threw away a perfectly readable
        # "ConnectionRefusedError: [Errno 111] Connect call failed".
        print(
            "Provisioning failed with an unexpected error. The traceback below is "
            "the only record of what went wrong.",
            file=sys.stderr,
        )
        traceback.print_exc()
        raise SystemExit(1) from None
    print(json.dumps(result.model_dump(mode="json"), sort_keys=True))


if __name__ == "__main__":
    main()
