from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import get_settings


class StorageError(Exception):
    """The object store could not complete an operation."""


class ObjectNotFoundError(StorageError):
    """The requested object does not exist."""


class ReadableObject(Protocol):
    def read(self, size: int | None = -1, /) -> bytes: ...

    def close(self) -> None: ...


class ObjectStorage(Protocol):
    def upload_file(
        self,
        path: Path,
        *,
        bucket: str,
        key: str,
        content_type: str,
        sha256: str,
    ) -> None: ...

    def open_object(self, *, bucket: str, key: str) -> ReadableObject: ...

    def delete_object(self, *, bucket: str, key: str) -> None: ...

    def check_health(self, *, bucket: str) -> None: ...


class S3ObjectStorage:
    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        region: str,
    ) -> None:
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(
                signature_version="s3v4",
                retries={"mode": "standard", "total_max_attempts": 4},
                s3={"addressing_style": "path"},
                connect_timeout=5,
                read_timeout=60,
            ),
        )

    def upload_file(
        self,
        path: Path,
        *,
        bucket: str,
        key: str,
        content_type: str,
        sha256: str,
    ) -> None:
        try:
            with path.open("rb") as source:
                self._client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=source,
                    ContentLength=path.stat().st_size,
                    ContentType=content_type,
                    Metadata={"sha256": sha256},
                    IfNoneMatch="*",
                )
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in {"PreconditionFailed", "412"}:
                self._verify_existing_object(
                    path, bucket=bucket, key=key, sha256=sha256
                )
                return
            raise StorageError("Object upload failed") from exc
        except (BotoCoreError, OSError) as exc:
            raise StorageError("Object upload failed") from exc

    def _verify_existing_object(
        self, path: Path, *, bucket: str, key: str, sha256: str
    ) -> None:
        try:
            response = self._client.head_object(Bucket=bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("Existing object could not be verified") from exc
        metadata = response.get("Metadata", {})
        if (
            metadata.get("sha256") != sha256
            or response.get("ContentLength") != path.stat().st_size
        ):
            raise StorageError("Object key collision detected")

    def open_object(self, *, bucket: str, key: str) -> ReadableObject:
        try:
            response = self._client.get_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in {"NoSuchKey", "NoSuchObject", "404"}:
                raise ObjectNotFoundError("Object does not exist") from exc
            raise StorageError("Object download failed") from exc
        except BotoCoreError as exc:
            raise StorageError("Object download failed") from exc
        return response["Body"]

    def delete_object(self, *, bucket: str, key: str) -> None:
        try:
            self._client.delete_object(Bucket=bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("Object deletion failed") from exc

    def check_health(self, *, bucket: str) -> None:
        try:
            self._client.head_bucket(Bucket=bucket)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("Object storage is unavailable") from exc


def iter_object(
    stream: ReadableObject, chunk_size: int = 1024 * 1024
) -> Iterator[bytes]:
    try:
        while chunk := stream.read(chunk_size):
            yield chunk
    finally:
        stream.close()


@lru_cache
def get_object_storage() -> ObjectStorage:
    settings = get_settings()
    return S3ObjectStorage(
        endpoint_url=settings.s3_endpoint_url,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key.get_secret_value(),
        region=settings.s3_region,
    )
