import hashlib
from pathlib import Path
from typing import Any

import pytest

from app.infrastructure.storage import (
    ObjectTooLargeError,
    S3ArtifactStorage,
    S3ObjectStorage,
)


class RecordingBody:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter(chunks)
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        del size
        return next(self._chunks, b"")

    def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self, body: RecordingBody, content_length: int) -> None:
        self.body = body
        self.content_length = content_length

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {"Body": self.body, "ContentLength": self.content_length}


def storage_with_client(client: FakeClient) -> S3ArtifactStorage:
    """The artifact client is the one that downloads; the raw client
    streams for the download route instead."""
    storage = object.__new__(S3ArtifactStorage)
    storage._client = client
    return storage


def test_download_hashes_bytes_and_closes_stream(tmp_path: Path) -> None:
    body = RecordingBody([b"abc", b"def"])
    storage = storage_with_client(FakeClient(body, 6))
    destination = tmp_path / "source.pdf"

    result = storage.download_file(
        destination, bucket="raw-documents", key="key", max_bytes=6
    )

    assert destination.read_bytes() == b"abcdef"
    assert result.sha256 == hashlib.sha256(b"abcdef").hexdigest()
    assert body.closed is True


def test_declared_oversized_download_closes_stream(tmp_path: Path) -> None:
    body = RecordingBody([b"unused"])
    storage = storage_with_client(FakeClient(body, 7))
    destination = tmp_path / "source.pdf"

    with pytest.raises(ObjectTooLargeError):
        storage.download_file(
            destination, bucket="raw-documents", key="key", max_bytes=6
        )

    assert body.closed is True
    assert not destination.exists()


def test_streamed_oversized_download_removes_partial_file(tmp_path: Path) -> None:
    body = RecordingBody([b"abcd", b"efgh"])
    storage = storage_with_client(FakeClient(body, 0))
    destination = tmp_path / "source.pdf"

    with pytest.raises(ObjectTooLargeError):
        storage.download_file(
            destination, bucket="raw-documents", key="key", max_bytes=6
        )

    assert body.closed is True
    assert not destination.exists()


@pytest.mark.parametrize(
    ("endpoint_url", "addressing_style"),
    [
        (None, "auto"),
        ("https://account.r2.cloudflarestorage.com", "path"),
    ],
)
def test_the_client_passes_provider_settings_to_boto3(
    monkeypatch: pytest.MonkeyPatch,
    endpoint_url: str | None,
    addressing_style: str,
) -> None:
    seen: dict[str, Any] = {}

    def fake_client(service_name: str, **kwargs: Any) -> object:
        seen["service_name"] = service_name
        seen["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("app.infrastructure.storage.boto3.client", fake_client)

    S3ObjectStorage(
        endpoint_url=endpoint_url,
        access_key="access-key",
        secret_key="secret-key",
        region="auto",
        addressing_style=addressing_style,
    )

    assert seen["service_name"] == "s3"
    kwargs = seen["kwargs"]
    assert kwargs["endpoint_url"] == endpoint_url
    assert kwargs["aws_access_key_id"] == "access-key"
    assert kwargs["aws_secret_access_key"] == "secret-key"
    assert kwargs["region_name"] == "auto"
    config = kwargs["config"]
    assert config.s3 == {"addressing_style": addressing_style}
    # R2 rejects the checksum headers boto3 adds by default.
    assert config.request_checksum_calculation == "when_required"
    assert config.response_checksum_validation == "when_required"
