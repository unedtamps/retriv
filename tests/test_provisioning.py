import hashlib
import json
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from app.core.config import Settings
from app.db.models import User, Workspace
from app.security.auth import (
    create_access_token,
    decode_access_token,
    verify_password,
)
from provisioning.models import ProvisioningRun
from provisioning.service import (
    MAX_REPORTED_PROBLEMS,
    ManifestConflictError,
    ManifestInProgressError,
    ManifestReadError,
    ManifestValidationError,
    ProvisionerSettings,
    ProvisioningError,
    apply_manifest,
    parse_manifest,
    read_manifest_object,
    render_failure,
)


def manifest_payload(
    *,
    migration_id: str = "bootstrap-1",
    username: str = "admin",
    password: str = "correct-password-123",
    role: str = "admin",
    is_active: bool = True,
    workspace_name: str = "Finance",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "migration_id": migration_id,
        "users": [
            {
                "username": username,
                "password": password,
                "role": role,
                "is_active": is_active,
            }
        ],
        "workspaces": [
            {
                "slug": "finance",
                "name": workspace_name,
                "description": "Finance workspace",
            }
        ],
    }


def make_manifest(payload: dict[str, Any]) -> tuple[bytes, str]:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    _, digest = parse_manifest(raw)
    return raw, digest


@pytest.mark.asyncio
async def test_apply_manifest_is_idempotent(
    db_session_factory: Any,
) -> None:
    manifest_payload_bytes, digest = make_manifest(manifest_payload())

    first = await apply_manifest(
        db_session_factory,
        manifest_payload=manifest_payload_bytes,
        source_uri="s3://migration-manifests/bootstrap/users-v1.json",
    )
    replay = await apply_manifest(
        db_session_factory,
        manifest_payload=manifest_payload_bytes,
        source_uri="s3://migration-manifests/bootstrap/users-v1.json",
    )

    assert first.users_created == 1
    assert first.workspaces_created == 1
    assert not first.already_applied
    assert replay.already_applied
    assert replay.users_created == 1
    assert replay.workspaces_created == 1

    async with db_session_factory() as session:
        assert (await session.scalar(select(User).where(User.username == "admin"))) is not None
        assert (
            await session.scalar(select(Workspace).where(Workspace.slug == "finance"))
        ) is not None
        assert (
            await session.scalar(
                select(ProvisioningRun).where(ProvisioningRun.migration_id == "bootstrap-1")
            )
        ) is not None


@pytest.mark.asyncio
async def test_new_manifest_id_upserts_existing_records(
    db_session_factory: Any,
) -> None:
    first_payload, first_digest = make_manifest(manifest_payload())
    await apply_manifest(
        db_session_factory,
        manifest_payload=first_payload,
        source_uri="s3://migration-manifests/bootstrap/users-v1.json",
    )
    async with db_session_factory() as session:
        created = await session.scalar(select(User).where(User.username == "admin"))
        assert created is not None
        assert created.auth_version == 0

    updated_payload, updated_digest = make_manifest(
        manifest_payload(
            migration_id="update-2",
            password="new-correct-password-123",
            role="auditor",
            is_active=False,
            workspace_name="Updated Finance",
        )
    )
    result = await apply_manifest(
        db_session_factory,
        manifest_payload=updated_payload,
        source_uri="s3://migration-manifests/bootstrap/users-v2.json",
    )

    assert result.users_updated == 1
    assert result.workspaces_updated == 1
    async with db_session_factory() as session:
        user = await session.scalar(select(User).where(User.username == "admin"))
        workspace = await session.scalar(select(Workspace).where(Workspace.slug == "finance"))
        assert user is not None
        assert workspace is not None
        assert user.role == "auditor"
        assert not user.is_active
        assert verify_password("new-correct-password-123", user.password_hash)
        assert user.auth_version == 1
        assert workspace.name == "Updated Finance"


@pytest.mark.asyncio
async def test_rotated_password_invalidates_previously_issued_token(
    db_session_factory: Any,
    settings: Settings,
) -> None:
    first_payload, first_digest = make_manifest(manifest_payload())
    await apply_manifest(
        db_session_factory,
        manifest_payload=first_payload,
        source_uri="s3://migration-manifests/bootstrap/users-v1.json",
    )
    async with db_session_factory() as session:
        user = await session.scalar(select(User).where(User.username == "admin"))
        assert user is not None
        token, _ = create_access_token(user, settings)

    rotated_payload, _ = make_manifest(
        manifest_payload(
            migration_id="rotation-2",
            password="rotated-password-123",
        )
    )
    await apply_manifest(
        db_session_factory,
        manifest_payload=rotated_payload,
        source_uri="s3://migration-manifests/bootstrap/users-v2.json",
    )

    claims = decode_access_token(token, settings)
    async with db_session_factory() as session:
        rotated = await session.scalar(select(User).where(User.username == "admin"))
        assert rotated is not None
        assert rotated.auth_version == claims.auth_version + 1


@pytest.mark.asyncio
async def test_same_migration_id_with_different_payload_is_rejected(
    db_session_factory: Any,
) -> None:
    first_payload, first_digest = make_manifest(manifest_payload())
    await apply_manifest(
        db_session_factory,
        manifest_payload=first_payload,
        source_uri="s3://migration-manifests/bootstrap/users-v1.json",
    )
    different_payload, different_digest = make_manifest(
        manifest_payload(password="different-password-123")
    )

    with pytest.raises(ManifestConflictError):
        await apply_manifest(
            db_session_factory,
            manifest_payload=different_payload,
            source_uri="s3://migration-manifests/bootstrap/users-v1.json",
        )


def test_parse_manifest_rejects_duplicates_and_unknown_fields() -> None:
    duplicate_payload = manifest_payload()
    duplicate_payload["users"].append(
        {
            "username": " ADMIN ",
            "password": "another-password-123",
            "role": "user",
        }
    )
    with pytest.raises(ProvisioningError):
        parse_manifest(json.dumps(duplicate_payload).encode())

    unknown_field_payload = manifest_payload()
    unknown_field_payload["unexpected"] = True
    with pytest.raises(ProvisioningError):
        parse_manifest(json.dumps(unknown_field_payload).encode())


def test_read_manifest_object_enforces_bucket_and_size(object_storage: Any) -> None:
    payload = json.dumps(manifest_payload()).encode()
    object_storage.objects[("migration-manifests", "bootstrap/users-v1.json")] = payload

    loaded_payload = read_manifest_object(
        object_storage,
        uri="s3://migration-manifests/bootstrap/users-v1.json",
        bucket="migration-manifests",
        prefix="bootstrap/",
        max_bytes=len(payload),
    )
    manifest, digest = parse_manifest(loaded_payload)

    assert manifest.migration_id == "bootstrap-1"
    assert digest == hashlib.sha256(payload).hexdigest()
    with pytest.raises(ProvisioningError):
        read_manifest_object(
            object_storage,
            uri="s3://raw-documents/bootstrap/users-v1.json",
            bucket="migration-manifests",
            prefix="bootstrap/",
            max_bytes=len(payload),
        )
    with pytest.raises(ProvisioningError):
        read_manifest_object(
            object_storage,
            uri="s3://migration-manifests/other/users-v1.json",
            bucket="migration-manifests",
            prefix="bootstrap/",
            max_bytes=len(payload),
        )


def test_provisioner_settings_reject_insecure_production_storage() -> None:
    with pytest.raises(ValidationError):
        ProvisionerSettings(
            environment="production",
            database_url="postgresql+asyncpg://provisioner:secret@postgres:5432/ingestion",
            s3_endpoint_url="http://minio:9000",
            s3_access_key="reader",
            s3_secret_key=SecretStr("reader-secret"),
            raw_document_bucket="raw-documents",
            manifest_bucket="migration-manifests",
            manifest_uri="s3://migration-manifests/bootstrap/users-v1.json",
        )


# --- error reporting -----------------------------------------------------
#
# This job is run by hand, so an error an operator cannot act on is worth
# about as much as no error at all. These tests pin the parts that make a
# failure actionable: which field, which S3 cause, and what to do next.


def test_validation_error_names_the_offending_field() -> None:
    payload = manifest_payload(password="short")
    with pytest.raises(ManifestValidationError) as caught:
        parse_manifest(json.dumps(payload).encode())

    rendered = render_failure(caught.value)
    assert "users[0].password" in rendered
    assert "at least 12 bytes" in rendered
    assert "PROVISIONER_MANIFEST_URI" in rendered


def test_validation_error_reports_every_problem_at_once() -> None:
    payload = manifest_payload()
    payload["users"][0]["password"] = "short"
    payload["users"][0]["role"] = "Not A Role"
    payload["workspaces"][0]["slug"] = "Not_A_Slug"

    with pytest.raises(ManifestValidationError) as caught:
        parse_manifest(json.dumps(payload).encode())

    problems = caught.value.problems
    assert len(problems) == 3, problems
    rendered = render_failure(caught.value)
    assert "users[0].password" in rendered
    assert "users[0].role" in rendered
    assert "workspaces[0].slug" in rendered


def test_validation_error_names_an_unknown_key() -> None:
    payload = manifest_payload()
    payload["unexpected"] = True
    with pytest.raises(ManifestValidationError) as caught:
        parse_manifest(json.dumps(payload).encode())
    assert "unexpected" in render_failure(caught.value)


def test_malformed_json_reports_where_it_broke() -> None:
    with pytest.raises(ManifestValidationError) as caught:
        parse_manifest(b'{"schema_version": 1, not json')
    rendered = render_failure(caught.value)
    # Pydantic's json_invalid carries a line and column, which is the whole
    # point of not flattening every failure into one sentence.
    assert "Invalid JSON" in rendered
    assert "line" in rendered


def test_validation_error_never_echoes_a_password() -> None:
    """The reason this cannot simply dump exc.errors().

    Pydantic puts the offending value in `input`, and some of these fields
    are passwords. Only `loc` and `msg` may reach the output.
    """
    secret = "hunter2xyz"
    assert len(secret) < 12
    payload = manifest_payload(password=secret)
    with pytest.raises(ManifestValidationError) as caught:
        parse_manifest(json.dumps(payload).encode())
    assert secret not in render_failure(caught.value)
    assert secret not in repr(caught.value.problems)


def test_validation_error_never_echoes_a_long_password_either() -> None:
    secret = "correct-horse-battery"
    payload = manifest_payload(password=secret[:5])
    with pytest.raises(ManifestValidationError) as caught:
        parse_manifest(json.dumps(payload).encode())
    assert secret not in render_failure(caught.value)


def test_validation_error_is_capped() -> None:
    payload = manifest_payload()
    payload["users"] = [
        {"username": f"user{index}", "password": "x", "role": "user"}
        for index in range(MAX_REPORTED_PROBLEMS + 3)
    ]
    with pytest.raises(ManifestValidationError) as caught:
        parse_manifest(json.dumps(payload).encode())

    rendered = render_failure(caught.value)
    assert "and 3 more" in rendered
    reported = [line for line in rendered.splitlines() if "password" in line]
    assert len(reported) == MAX_REPORTED_PROBLEMS
    assert "users[0].password" in reported[0]
    assert f"users[{MAX_REPORTED_PROBLEMS - 1}].password" in reported[-1]


class RaisingStorage:
    """Stands in for object storage by raising a chosen S3 failure."""

    def __init__(self, code: str | None) -> None:
        self.code = code

    def upload_file(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("not reached")

    def delete_object(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("not reached")

    def check_health(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise AssertionError("not reached")

    def presign_get(self, *, bucket: str, key: str, expires_in: int) -> str:
        raise AssertionError("not reached")

    def open_object(self, *, bucket: str, key: str) -> Any:
        from botocore.exceptions import ClientError

        from app.infrastructure.storage import (
            ObjectNotFoundError,
            StorageError,
        )

        if self.code is None:
            # What the storage layer raises when MinIO is unreachable:
            # BotoCoreError wrapped, with no ClientError to recover a code from.
            raise StorageError("Object storage is unavailable") from OSError(
                "connection refused"
            )
        cause = ClientError({"Error": {"Code": self.code}}, "GetObject")
        if self.code in {"NoSuchKey", "NoSuchBucket"}:
            raise ObjectNotFoundError("does not exist") from cause
        raise StorageError("download failed") from cause


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("NoSuchKey", "upload the manifest"),
        ("NoSuchBucket", "make infra"),
        ("AccessDenied", "PROVISIONER_MANIFEST_PREFIX"),
        ("SignatureDoesNotMatch", "PROVISIONER_S3_SECRET_KEY"),
        ("InvalidAccessKeyId", "PROVISIONER_S3_ACCESS_KEY"),
    ],
)
def test_read_failures_are_told_apart(code: str, expected: str) -> None:
    """Each cause needs a different fix, so each gets its own hint.

    Before this, all five produced "Manifest object could not be read".
    """
    with pytest.raises(ManifestReadError) as caught:
        read_manifest_object(
            RaisingStorage(code),
            uri="s3://migration-manifests/bootstrap/users-v1.json",
            bucket="migration-manifests",
            prefix="bootstrap/",
            max_bytes=1024,
        )
    rendered = render_failure(caught.value)
    assert caught.value.s3_code == code
    assert f"s3 code  {code}" in rendered
    assert expected in rendered


def test_unreachable_object_store_is_not_blamed_on_the_manifest() -> None:
    with pytest.raises(ManifestReadError) as caught:
        read_manifest_object(
            RaisingStorage(None),
            uri="s3://migration-manifests/bootstrap/users-v1.json",
            bucket="migration-manifests",
            prefix="bootstrap/",
            max_bytes=1024,
        )
    assert caught.value.s3_code is None
    assert caught.value.hint is not None
    assert "PROVISIONER_S3_ENDPOINT_URL" in caught.value.hint


def test_manifest_uri_errors_say_what_to_edit() -> None:
    with pytest.raises(ProvisioningError) as wrong_bucket:
        read_manifest_object(
            RaisingStorage("NoSuchKey"),
            uri="s3://raw-documents/bootstrap/users-v1.json",
            bucket="migration-manifests",
            prefix="bootstrap/",
            max_bytes=1024,
        )
    assert "PROVISIONER_MANIFEST_BUCKET" in str(wrong_bucket.value)
    assert wrong_bucket.value.hint is not None

    with pytest.raises(ProvisioningError) as wrong_prefix:
        read_manifest_object(
            RaisingStorage("NoSuchKey"),
            uri="s3://migration-manifests/other/users-v1.json",
            bucket="migration-manifests",
            prefix="bootstrap/",
            max_bytes=1024,
        )
    assert "PROVISIONER_MANIFEST_PREFIX" in str(wrong_prefix.value)


def test_settings_report_every_missing_variable_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first run with nothing set used to take one run per variable."""
    for name in (
        "PROVISIONER_DATABASE_URL",
        "PROVISIONER_S3_ENDPOINT_URL",
        "PROVISIONER_S3_ACCESS_KEY",
        "PROVISIONER_S3_SECRET_KEY",
        "PROVISIONER_RAW_DOCUMENT_BUCKET",
        "PROVISIONER_MANIFEST_URI",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError) as caught:
        ProvisionerSettings()
    message = str(caught.value)
    assert message.count("is required") >= 4, message


def test_conflict_and_in_progress_name_the_migration_id() -> None:
    conflict = ManifestConflictError("bootstrap-2")
    assert "bootstrap-2" in render_failure(conflict)
    assert "immutable" in render_failure(conflict)

    running = ManifestInProgressError("bootstrap-2")
    assert "bootstrap-2" in render_failure(running)
    assert "advisory lock" in render_failure(running)


def test_database_error_keeps_only_the_line_with_the_cause() -> None:
    """asyncpg and the pool wrap a refused socket in several lines of
    context; only the last one says what actually went wrong."""
    from provisioning.service import _first_line

    chained = OSError("[Errno 111] Connect call failed ('127.0.0.1', 5999)")
    chained.__cause__ = RuntimeError(
        "connect\ncould not resolve host\nretrying\ntimed out"
    )
    assert _first_line(chained).startswith("[Errno 111]")
    assert _first_line(RuntimeError("plain")) == "plain"
    assert _first_line(RuntimeError()) == "RuntimeError"


def test_unreachable_database_is_reported_not_dumped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database that is simply not running must not reach the traceback
    handler. Caught here because SQLAlchemyError alone does not catch a
    refused socket: asyncpg raises the OSError unwrapped."""
    import asyncio

    import provisioning.service as service_module
    from provisioning.service import DatabaseUnreachableError, run_provisioning

    # The manifest is read before the database is touched. Stub that one
    # step so this test isolates the database probe and needs no live infra.
    monkeypatch.setattr(
        service_module,
        "read_manifest_object",
        lambda *args, **kwargs: json.dumps(manifest_payload()).encode(),
    )

    settings = ProvisionerSettings(
        database_url="postgresql+asyncpg://retriv:x@127.0.0.1:1/retriv",
        s3_endpoint_url="http://127.0.0.1:9000",
        s3_access_key="reader",
        s3_secret_key=SecretStr("reader-secret"),
        raw_document_bucket="raw-documents",
        manifest_uri="s3://migration-manifests/bootstrap/users-v1.json",
    )
    with pytest.raises(DatabaseUnreachableError) as caught:
        asyncio.run(run_provisioning(settings))

    rendered = render_failure(caught.value)
    assert "database could not be reached" in rendered
    assert caught.value.hint is not None
    assert "PROVISIONER_DATABASE_URL" in caught.value.hint
    # A traceback here would bury a one-line cause in pool internals.
    assert "Traceback" not in rendered
