import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
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


class ObjectTooLargeError(StorageError):
    """The requested object exceeds the configured processing limit."""


@dataclass(frozen=True)
class DownloadResult:
    size_bytes: int
    sha256: str


class ReadableObject(Protocol):
    def read(self, size: int | None = -1, /) -> bytes: ...

    def close(self) -> None: ...


class ObjectStorage(Protocol):
    """Raw-document access, used by the upload and download routes."""

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


class ArtifactStorage(Protocol):
    """Extracted-artifact access, used by the worker.

    Separate from ObjectStorage because the two hold different MinIO
    identities: raw documents and extracted artifacts live in different
    buckets with different IAM policies.
    """

    def download_file(
        self,
        destination: Path,
        *,
        bucket: str,
        key: str,
        max_bytes: int,
    ) -> DownloadResult: ...

    def put_bytes(
        self,
        data: bytes,
        *,
        bucket: str,
        key: str,
        content_type: str,
        content_encoding: str | None,
        sha256: str,
    ) -> None: ...

    def delete_object(self, *, bucket: str, key: str) -> None: ...

    def delete_prefix(self, *, bucket: str, prefix: str) -> None: ...


class _S3Client:
    """Shared boto3 plumbing for both storage views."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        region: str,
        read_timeout: int,
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
                read_timeout=read_timeout,
            ),
        )

    @staticmethod
    def _is_missing(exc: ClientError) -> bool:
        return exc.response.get("Error", {}).get("Code", "") in {
            "NoSuchKey",
            "NoSuchObject",
            "404",
        }


class S3ObjectStorage(_S3Client):
    """Raw-document bucket: written on upload, read on download."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        region: str,
    ) -> None:
        super().__init__(
            endpoint_url=endpoint_url,
            access_key=access_key,
            secret_key=secret_key,
            region=region,
            read_timeout=60,
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
            if self._is_missing(exc):
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


class S3ArtifactStorage(_S3Client):
    """Artifact bucket: read by the worker, never written by the API."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        region: str,
    ) -> None:
        super().__init__(
            endpoint_url=endpoint_url,
            access_key=access_key,
            secret_key=secret_key,
            region=region,
            read_timeout=120,
        )

    def download_file(
        self,
        destination: Path,
        *,
        bucket: str,
        key: str,
        max_bytes: int,
    ) -> DownloadResult:
        stream: Any | None = None
        try:
            response = self._client.get_object(Bucket=bucket, Key=key)
            stream = response["Body"]
            assert stream is not None
            content_length = int(response.get("ContentLength", 0))
            if content_length > max_bytes:
                raise ObjectTooLargeError("Source object exceeds the processing limit")
            digest = hashlib.sha256()
            total_bytes = 0
            with destination.open("wb") as output:
                while chunk := stream.read(1024 * 1024):
                    total_bytes += len(chunk)
                    if total_bytes > max_bytes:
                        raise ObjectTooLargeError(
                            "Source object exceeds the processing limit"
                        )
                    digest.update(chunk)
                    output.write(chunk)
            return DownloadResult(size_bytes=total_bytes, sha256=digest.hexdigest())
        except ObjectTooLargeError:
            destination.unlink(missing_ok=True)
            raise
        except ClientError as exc:
            destination.unlink(missing_ok=True)
            if self._is_missing(exc):
                raise ObjectNotFoundError("Source object does not exist") from exc
            raise StorageError("Object download failed") from exc
        except (BotoCoreError, OSError) as exc:
            destination.unlink(missing_ok=True)
            raise StorageError("Object download failed") from exc
        finally:
            if stream is not None:
                stream.close()

    def put_bytes(
        self,
        data: bytes,
        *,
        bucket: str,
        key: str,
        content_type: str,
        content_encoding: str | None,
        sha256: str,
    ) -> None:
        request: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Body": data,
            "ContentLength": len(data),
            "ContentType": content_type,
            "Metadata": {"sha256": sha256},
        }
        if content_encoding is not None:
            request["ContentEncoding"] = content_encoding
        try:
            self._client.put_object(**request)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("Artifact upload failed") from exc

    def delete_object(self, *, bucket: str, key: str) -> None:
        try:
            self._client.delete_object(Bucket=bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("Artifact deletion failed") from exc

    def delete_prefix(self, *, bucket: str, prefix: str) -> None:
        try:
            while True:
                page = self._client.list_objects_v2(
                    Bucket=bucket,
                    Prefix=prefix,
                    MaxKeys=1000,
                )
                objects = [
                    {"Key": item["Key"]}
                    for item in page.get("Contents", [])
                    if item.get("Key")
                ]
                if not objects:
                    return
                response = self._client.delete_objects(
                    Bucket=bucket,
                    Delete={"Objects": objects, "Quiet": True},
                )
                if response.get("Errors"):
                    raise StorageError("Artifact prefix deletion was incomplete")
        except (BotoCoreError, ClientError) as exc:
            raise StorageError("Artifact prefix deletion failed") from exc


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


@lru_cache
def get_artifact_storage() -> ArtifactStorage:
    """The worker's object-store view.

    Uses the artifact identity, which can read raw documents and write
    artifacts but cannot write raw documents.
    """
    settings = get_settings()
    return S3ArtifactStorage(
        endpoint_url=settings.s3_endpoint_url,
        access_key=settings.s3_artifact_access_key,
        secret_key=settings.s3_artifact_secret_key.get_secret_value(),
        region=settings.s3_region,
    )
