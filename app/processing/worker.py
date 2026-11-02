import asyncio
import csv
import gzip
import hashlib
import io
import json
import logging
import os
import shutil
import struct
import tempfile
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from anyio import to_thread
from opentelemetry.trace import Status, StatusCode
from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import (
    ACTIVE_RUN_STATUSES,
    EMBEDDING_FAILED,
    EMBEDDING_INDEXED,
    EMBEDDING_PENDING,
    EMBEDDING_PROCESSING,
    EMBEDDING_RETRY_WAIT,
    JOB_ACCESS,
    JOB_DELETE,
    JOB_EXTRACT,
    RUN_CANCELLED,
    RUN_CHUNKING,
    RUN_DELETING,
    RUN_DESCRIBING,
    RUN_EMBEDDING,
    RUN_EXTRACTING,
    RUN_FAILED,
    RUN_FETCHING,
    RUN_QUEUED,
    RUN_READY,
    RUN_RETRY_WAIT,
    RUN_STORING_ARTIFACT,
    RUN_SYNCING,
    ChunkEmbedding,
    Document,
    DocumentChunk,
    DocumentImage,
    DocumentRow,
    DocumentWorkspace,
    ProcessingRun,
)
from app.documents import conversion
from app.documents.validation import (
    CSV_ENCODINGS,
    CSV_MEDIA_TYPE,
    DOCX_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    PPTX_MEDIA_TYPE,
    XLSX_MEDIA_TYPE,
    sniff_delimiter,
    uniquify_columns,
)
from app.embeddings.base import (
    EmbeddingProvider,
    EmbeddingProviderError,
    VectorPoint,
    VectorStore,
    VectorStoreError,
)
from app.extraction.base import (
    DocumentExtractor,
    ExtractedChunk,
    ExtractedImage,
    ExtractionError,
    ExtractionResourceError,
    ExtractionResult,
)
from app.extraction.images import (
    IMAGE_SUFFIXES,
    is_no_figure_reply,
    merge_descriptions,
    picture_refs,
)
from app.extraction.xlsx_reader import (
    SheetPicture,
    SheetTable,
    WorkbookError,
    WorkbookTooLargeError,
    read_workbook,
)
from app.infrastructure.storage import (
    ArtifactStorage,
    ObjectNotFoundError,
    ObjectTooLargeError,
    StorageError,
)
from app.observability.tracing import get_tracer
from app.vision.base import VisionProvider, VisionProviderError, build_context

logger = logging.getLogger(__name__)
_tracer = get_tracer(__name__)

_SOURCE_SUFFIX = {
    PDF_MEDIA_TYPE: ".pdf",
    DOCX_MEDIA_TYPE: ".docx",
    PPTX_MEDIA_TYPE: ".pptx",
}

_ROW_INSERT_BATCH = 1_000

_IMAGE_PREVIEW_EDGE = 1568
"""Longest side of the preview an agent is served by default.

Vision models tokenize by pixels and downscale beyond this edge themselves,
so a larger file buys nothing but transfer time."""

_PREVIEW_JPEG_QUALITY = 85
"""Quality for a re-encoded JPEG preview. Visually indistinguishable from the
source at this level, and several times smaller than the lossless PNG that
storing photographic pixels exactly would produce."""

_LOSSY_IMAGE_FORMATS = frozenset({b"jpeg"})
"""Source formats that stay lossy on the way out. PNG, GIF and TIFF are kept
lossless: they are what screenshots, charts and diagrams arrive as, and the
compression artefacts of a lossy re-encode blur exactly the small text a
reader needs."""

_IMAGE_SIGNATURES: tuple[tuple[bytes, bytes], ...] = (
    (b"\xff\xd8\xff", b"jpeg"),
    (b"\x89PNG\r\n\x1a\n", b"png"),
    (b"GIF8", b"gif"),
    (b"II*\x00", b"tiff"),
    (b"MM\x00*", b"tiff"),
)


def _image_format_of(data: bytes) -> bytes | None:
    """The picture's own format, read from its signature bytes."""
    for signature, name in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return name
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return b"webp"
    return None


def _image_pixel_size(data: bytes, image_format: bytes) -> tuple[int, int] | None:
    """The bitmap's width and height in pixels, straight from its header.

    pymupdf answers in points scaled by the embedded DPI, which is off by the
    ratio between the two; the header carries the real pixel count.
    """
    if image_format == b"png" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return (width, height) if width and height else None
    if image_format == b"gif" and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return (width, height) if width and height else None
    if image_format == b"jpeg":
        return _jpeg_pixel_size(data)
    return None


def _jpeg_pixel_size(data: bytes) -> tuple[int, int] | None:
    """Walk the JPEG marker chain to the frame header, which holds the size."""
    index = 2
    length = len(data)
    while index + 9 < length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        segment_length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        # SOF0..SOF15, excluding the non-frame markers DHT/JPG/DAC.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return (width, height) if width and height else None
        index += 2 + segment_length
    return None


def image_object_keys(ref: str, media_type: str, images_prefix: str) -> str:
    """The artifact key a picture is stored under: a render within the edge
    a vision model reads at, or the untouched bytes when it cannot be
    re-read."""
    suffix = IMAGE_SUFFIXES.get(media_type, ".bin")
    return f"{images_prefix}/images/{ref}{suffix}"


class PermanentProcessingError(Exception):
    """A processing retry cannot fix this failure."""


class LeaseLostError(Exception):
    """Another worker owns the processing run lease."""


def utcnow() -> datetime:
    return datetime.now(UTC)


class ProcessingWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        storage: ArtifactStorage,
        extractor: DocumentExtractor,
        settings: Settings,
        embedding_provider: EmbeddingProvider,
        vector_store: VectorStore,
        vision_provider: VisionProvider | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._storage = storage
        self._extractor = extractor
        self._settings = settings
        self._embedding_provider = embedding_provider
        self._vector_store = vector_store
        self._vision_provider = vision_provider
        self._next_artifact_reconciliation = datetime.min.replace(tzinfo=UTC)
        self._next_embedding_reconciliation = datetime.min.replace(tzinfo=UTC)
        self._extraction_slots = asyncio.Semaphore(settings.parser_max_concurrent_extractions)
        self._vlm_slots = asyncio.Semaphore(settings.vlm_max_concurrency)
        self._in_flight: set[asyncio.Task[None]] = set()
        self._held_leases: dict[UUID, UUID] = {}

    async def run_once(self) -> bool:
        await self._recover_expired_leases()
        await self._reconcile_deleted_artifacts()
        await self._queue_embedding_backfill()
        run = await self._claim_one()
        if run is None:
            return False
        await self._process_claimed(run)
        return True

    async def _process_claimed(self, run: ProcessingRun) -> None:
        """Carry one claimed run through its whole pipeline.

        The umbrella heartbeat covers stages with no heartbeat of their own,
        including the wait for an extraction slot. It only extends
        ``lease_until``, like the stage heartbeats, so two writers are one.
        """
        with _tracer.start_as_current_span(
            "process.run",
            attributes={
                "retriv.run_id": str(run.id),
                "retriv.document_id": str(run.document_id),
                "retriv.job_kind": run.job_kind,
            },
        ) as span:
            stopped = asyncio.Event()
            heartbeat = asyncio.create_task(self._heartbeat_lease(run.id, run.lease_token, stopped))
            if run.lease_token is not None:
                self._held_leases[run.id] = run.lease_token
            try:
                if run.job_kind == JOB_DELETE:
                    await self._process_delete(run)
                elif run.job_kind == JOB_ACCESS:
                    await self._process_access_sync(run)
                else:
                    await self._process_extract(run)
            except LeaseLostError as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, "lease lost"))
                logger.warning("Processing lease was lost", extra={"run_id": str(run.id)})
            except (
                PermanentProcessingError,
                ObjectNotFoundError,
                ObjectTooLargeError,
                WorkbookError,
            ) as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                await self._record_failure(run, exc, permanent=True)
            except (EmbeddingProviderError, VectorStoreError, VisionProviderError) as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                await self._record_failure(run, exc, permanent=not exc.retryable)
            except (StorageError, ExtractionError, OSError, ValueError) as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                await self._record_failure(run, exc, permanent=False)
            except Exception as exc:
                logger.exception("Unexpected processing failure", extra={"run_id": str(run.id)})
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                await self._record_failure(run, exc, permanent=False)
            finally:
                self._held_leases.pop(run.id, None)
                stopped.set()
                await heartbeat

    async def run(
        self, stop_event: asyncio.Event, *, shutdown_grace_seconds: float | None = None
    ) -> None:
        """Claim and carry runs concurrently until asked to stop.

        The sweeps run only here, on the loop: the reconciliation
        timestamps they advance are not safe to touch from two places.

        Stopping drains -- no new claims. With ``shutdown_grace_seconds`` unset
        the loop waits for every in-flight run, which is what a test or a
        deliberate stop wants. The container passes a number, because Docker
        stops waiting after its own grace period: anything still running then
        is abandoned and its lease handed back, instead of dying with the lease
        still written to the row.
        """
        abandoned = False
        while not stop_event.is_set() or (self._in_flight and not abandoned):
            if stop_event.is_set() and self._in_flight and not abandoned:
                pending = asyncio.shield(
                    asyncio.gather(*self._in_flight, return_exceptions=True)
                )
                try:
                    if shutdown_grace_seconds is None:
                        await pending
                    else:
                        await asyncio.wait_for(pending, timeout=shutdown_grace_seconds)
                except TimeoutError:
                    logger.warning(
                        "Shutdown grace expired with %s run(s) still in flight; "
                        "handing their leases back",
                        len(self._in_flight),
                        extra={"retriv.abandoned_runs": len(self._in_flight)},
                    )
                    # Hand the leases back BEFORE cancelling. A run blocked in
                    # the extraction sandbox sits in a worker thread that cannot
                    # be interrupted, so awaiting the cancelled tasks would wait
                    # for the sandbox to finish and the container would be
                    # SIGKILLed first -- leaving the lease written. Cancelling
                    # afterwards is best-effort: the process is about to exit and
                    # any writer that still wakes finds its lease gone.
                    await self.release_held_leases()
                    for task in list(self._in_flight):
                        task.cancel()
                    abandoned = True
                continue
            if not stop_event.is_set():
                await self._recover_expired_leases()
                await self._reconcile_deleted_artifacts()
                await self._queue_embedding_backfill()
                while len(self._in_flight) < self._settings.processing_max_concurrent_runs:
                    run = await self._claim_one()
                    if run is None:
                        break
                    task = asyncio.create_task(self._process_claimed(run))
                    self._in_flight.add(task)
                    task.add_done_callback(self._in_flight.discard)
            if not self._in_flight:
                if stop_event.is_set():
                    return
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self._settings.processing_poll_interval_seconds,
                    )
                except TimeoutError:
                    continue
                continue
            done, _ = await asyncio.wait(
                self._in_flight,
                timeout=self._settings.processing_poll_interval_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    logger.error(
                        "Processing task failed unexpectedly",
                        exc_info=task.exception(),
                    )

    async def release_held_leases(self, *, held: dict[UUID, UUID] | None = None) -> int:
        """Hand back every lease this process still holds, without finishing it.

        A deployment restarts the worker, and Docker sends SIGTERM with a ten
        second grace period before SIGKILL. An extraction sandbox runs far
        longer than that, so the process dies mid-stage with the lease still
        written to the row: the run sits in an active status for the full
        ``processing_lease_seconds`` while no worker owns it -- half an hour
        of a document that looks stuck in `fetching`.

        Returning the row to the queue shortens that to however long the next
        claim takes. The lease token is the guard: only the attempt that still
        owns the row can release it, so a worker that lost its lease to a
        recovery sweep cannot knock the new owner off.
        """
        released = 0
        leases = dict(self._held_leases) if held is None else held
        for run_id, lease_token in leases.items():
            try:
                async with self._session_factory() as session:
                    result = await session.execute(
                        update(ProcessingRun)
                        .where(
                            ProcessingRun.id == run_id,
                            ProcessingRun.lease_token == lease_token,
                            ProcessingRun.status.in_(ACTIVE_RUN_STATUSES),
                        )
                        .values(
                            status=RUN_RETRY_WAIT,
                            available_at=utcnow(),
                            lease_until=None,
                            lease_token=None,
                            last_error="Worker stopped before finishing",
                        )
                    )
                    await session.commit()
                    if getattr(result, "rowcount", 0) == 1:
                        released += 1
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.warning(
                    "Could not release the lease of a run being abandoned",
                    extra={"run_id": str(run_id)},
                    exc_info=True,
                )
            finally:
                if held is None:
                    self._held_leases.pop(run_id, None)
        return released

    async def _recover_expired_leases(self) -> None:
        now = utcnow()
        async with self._session_factory() as session, session.begin():
            expired = (
                await session.execute(
                    select(ProcessingRun.id, ProcessingRun.attempts).where(
                        ProcessingRun.status.in_(ACTIVE_RUN_STATUSES),
                        ProcessingRun.lease_until.is_not(None),
                        ProcessingRun.lease_until < now,
                    )
                )
            ).all()
            if not expired:
                return
            failed_ids = [
                run_id
                for run_id, attempts in expired
                if attempts >= self._settings.processing_max_attempts
            ]
            retry_ids = [
                run_id
                for run_id, attempts in expired
                if attempts < self._settings.processing_max_attempts
            ]
            if failed_ids:
                await session.execute(
                    update(ProcessingRun)
                    .where(
                        ProcessingRun.status.in_(ACTIVE_RUN_STATUSES),
                        ProcessingRun.lease_until.is_not(None),
                        ProcessingRun.lease_until < now,
                        ProcessingRun.attempts >= self._settings.processing_max_attempts,
                    )
                    .values(
                        status=RUN_FAILED,
                        lease_until=None,
                        lease_token=None,
                        completed_at=now,
                        last_error="Processing lease expired at the retry limit",
                    )
                )
            if retry_ids:
                await session.execute(
                    update(ProcessingRun)
                    .where(
                        ProcessingRun.status.in_(ACTIVE_RUN_STATUSES),
                        ProcessingRun.lease_until.is_not(None),
                        ProcessingRun.lease_until < now,
                        ProcessingRun.attempts < self._settings.processing_max_attempts,
                    )
                    .values(
                        status=RUN_RETRY_WAIT,
                        available_at=now,
                        lease_until=None,
                        lease_token=None,
                        last_error="Processing lease expired before completion",
                    )
                )
            if failed_ids:
                await session.execute(
                    update(ChunkEmbedding)
                    .where(
                        ChunkEmbedding.processing_run_id.in_(failed_ids),
                        ChunkEmbedding.status != EMBEDDING_INDEXED,
                    )
                    .values(
                        status=EMBEDDING_FAILED,
                        last_error="Processing lease expired at the retry limit",
                        updated_at=now,
                    )
                )
            if retry_ids:
                await session.execute(
                    update(ChunkEmbedding)
                    .where(
                        ChunkEmbedding.processing_run_id.in_(retry_ids),
                        ChunkEmbedding.status != EMBEDDING_INDEXED,
                    )
                    .values(
                        status=EMBEDDING_RETRY_WAIT,
                        last_error="Processing lease expired before completion",
                        updated_at=now,
                    )
                )

    async def _reconcile_deleted_artifacts(self) -> None:
        """Repair whatever a lost or failed delete run left behind.

        Every document carrying a tombstone is swept on each interval: the
        artifact folder, the raw upload, and the vectors. Each step is
        idempotent, so a document whose delete run is still queued is
        merely cleaned early, and one whose run failed permanently is
        cleaned here instead of leaking forever -- an object store delete
        that succeeds on a missing key costs one list.
        """
        now = utcnow()
        if now < self._next_artifact_reconciliation:
            return
        self._next_artifact_reconciliation = now + timedelta(
            seconds=self._settings.artifact_reconciliation_interval_seconds
        )
        async with self._session_factory() as session:
            result = await session.execute(
                select(Document.origin_workspace_id, Document.sha256, Document.id).where(
                    Document.deleted_at.is_not(None)
                )
            )
            tombstones = result.all()
        for workspace_id, sha256, document_id in tombstones:
            prefix = f"workspaces/{workspace_id}/documents/{document_id}/"
            try:
                await to_thread.run_sync(
                    partial(
                        self._storage.delete_prefix,
                        bucket=self._settings.s3_artifact_bucket,
                        prefix=prefix,
                    )
                )
            except StorageError:
                logger.warning(
                    "Deleted-document artifact reconciliation failed",
                    extra={
                        "workspace_id": str(workspace_id),
                        "document_id": str(document_id),
                    },
                    exc_info=True,
                )
            try:
                await to_thread.run_sync(
                    partial(
                        self._storage.delete_object,
                        bucket=self._settings.s3_raw_bucket,
                        key=f"workspaces/{workspace_id}/objects/{sha256}/{document_id}",
                    )
                )
            except StorageError:
                logger.warning(
                    "Deleted-document raw object reconciliation failed",
                    extra={
                        "workspace_id": str(workspace_id),
                        "document_id": str(document_id),
                    },
                    exc_info=True,
                )
            try:
                await self._vector_store.delete_document(document_id)
            except VectorStoreError:
                logger.warning(
                    "Deleted-document vector reconciliation failed",
                    extra={
                        "workspace_id": str(workspace_id),
                        "document_id": str(document_id),
                    },
                    exc_info=True,
                )

    async def _queue_embedding_backfill(self) -> None:
        now = utcnow()
        if now < self._next_embedding_reconciliation:
            return
        self._next_embedding_reconciliation = now + timedelta(
            seconds=self._settings.embedding_reconciliation_interval_seconds
        )
        settings = self._settings
        async with self._session_factory() as session, session.begin():
            runs = (
                await session.scalars(
                    select(ProcessingRun)
                    .join(Document, Document.id == ProcessingRun.document_id)
                    .where(
                        ProcessingRun.job_kind == JOB_EXTRACT,
                        ProcessingRun.status == RUN_READY,
                        Document.deleted_at.is_(None),
                        Document.active_processing_run_id == ProcessingRun.id,
                        ProcessingRun.chunk_count > 0,
                        or_(
                            ProcessingRun.embedded_chunk_count != ProcessingRun.chunk_count,
                            ProcessingRun.embedding_profile.is_(None),
                            ProcessingRun.embedding_profile != settings.embedding_profile,
                            ProcessingRun.embedding_model.is_(None),
                            ProcessingRun.embedding_model != settings.embedding_model,
                            ProcessingRun.embedding_dimensions.is_(None),
                            ProcessingRun.embedding_dimensions != settings.embedding_dimensions,
                        ),
                    )
                    .order_by(ProcessingRun.created_at, ProcessingRun.id)
                    .limit(100)
                    .with_for_update(of=ProcessingRun, skip_locked=True)
                )
            ).all()
            for run in runs:
                chunks = (
                    await session.execute(
                        select(
                            DocumentChunk.id,
                            DocumentChunk.origin_workspace_id,
                            DocumentChunk.document_id,
                            DocumentChunk.processing_run_id,
                            DocumentChunk.content_hash,
                        ).where(DocumentChunk.processing_run_id == run.id)
                    )
                ).all()
                if not chunks:
                    continue
                embeddings = (
                    await session.scalars(
                        select(ChunkEmbedding).where(
                            ChunkEmbedding.processing_run_id == run.id,
                            ChunkEmbedding.embedding_profile == settings.embedding_profile,
                        )
                    )
                ).all()
                by_chunk_id = {embedding.chunk_id: embedding for embedding in embeddings}
                indexed_count = 0
                for chunk in chunks:
                    embedding = by_chunk_id.get(chunk.id)
                    if embedding is None:
                        session.add(
                            self._new_embedding_record(
                                chunk_id=chunk.id,
                                origin_workspace_id=chunk.origin_workspace_id,
                                document_id=chunk.document_id,
                                processing_run_id=chunk.processing_run_id,
                                content_hash=chunk.content_hash,
                            )
                        )
                        continue
                    if (
                        embedding.status == EMBEDDING_INDEXED
                        and embedding.content_hash == chunk.content_hash
                        and embedding.model == settings.embedding_model
                        and embedding.dimensions == settings.embedding_dimensions
                    ):
                        indexed_count += 1
                        continue
                    embedding.model = settings.embedding_model
                    embedding.dimensions = settings.embedding_dimensions
                    embedding.content_hash = chunk.content_hash
                    embedding.qdrant_point_id = self._qdrant_point_id(chunk.id)
                    embedding.status = EMBEDDING_PENDING
                    embedding.last_error = None
                    embedding.embedded_at = None
                    embedding.updated_at = now
                run.chunk_count = len(chunks)
                run.embedded_chunk_count = indexed_count
                run.embedding_profile = settings.embedding_profile
                run.embedding_model = settings.embedding_model
                run.embedding_dimensions = settings.embedding_dimensions
                if indexed_count == len(chunks):
                    continue
                run.status = RUN_QUEUED
                run.available_at = now
                run.completed_at = None
                run.last_error = None

    async def _claim_one(self) -> ProcessingRun | None:
        now = utcnow()
        lease_until = now + timedelta(seconds=self._settings.processing_lease_seconds)
        async with self._session_factory() as session, session.begin():
            run = await session.scalar(
                select(ProcessingRun)
                .where(
                    ProcessingRun.status.in_((RUN_QUEUED, RUN_RETRY_WAIT)),
                    ProcessingRun.available_at <= now,
                )
                .order_by(ProcessingRun.created_at, ProcessingRun.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if run is None:
                return None
            if run.job_kind == JOB_DELETE:
                run.status = RUN_DELETING
            elif run.job_kind == JOB_ACCESS:
                run.status = RUN_SYNCING
            elif run.chunk_count and run.artifact_key and run.manifest_key:
                run.status = RUN_EMBEDDING
            else:
                run.status = RUN_FETCHING
            run.attempts += 1
            run.lease_until = lease_until
            run.lease_token = uuid4()
            run.started_at = run.started_at or now
            run.last_error = None
            return run

    async def _process_extract(self, claimed_run: ProcessingRun) -> None:
        lease_token = self._require_lease_token(claimed_run)
        if claimed_run.status == RUN_EMBEDDING:
            await self._process_embeddings(claimed_run, lease_token)
            return
        await self._discard_previous_attempt_artifacts(claimed_run, lease_token)
        document = await self._load_document(claimed_run)
        if document.deleted_at is not None:
            await self._cancel_run(claimed_run.id, lease_token)
            return
        if document.bucket != self._settings.s3_raw_bucket:
            raise PermanentProcessingError("Document references an unapproved raw bucket")
        expected_key = (
            f"workspaces/{document.origin_workspace_id}/objects/{document.sha256}/{document.id}"
        )
        if document.object_key != expected_key:
            raise PermanentProcessingError("Object key does not match document identity")
        if document.size_bytes > self._settings.max_source_bytes:
            raise PermanentProcessingError("Document exceeds the processing size limit")

        if document.detected_media_type == CSV_MEDIA_TYPE:
            async with self._extraction_slots:
                await self._process_csv_extract(claimed_run, lease_token, document)
            return

        if document.detected_media_type == XLSX_MEDIA_TYPE:
            async with self._extraction_slots:
                await self._process_xlsx_extract(claimed_run, lease_token, document)
            return

        self._settings.processing_temp_dir.mkdir(parents=True, exist_ok=True)
        suffix = _SOURCE_SUFFIX.get(document.detected_media_type)
        if suffix is None:
            raise PermanentProcessingError(
                f"No source suffix for media type: {document.detected_media_type}"
            )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="context-source-",
            suffix=suffix,
            dir=self._settings.processing_temp_dir,
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        extraction_path = temporary_path
        artifact_key: str | None = None
        manifest_key: str | None = None
        chunks_persisted = False
        try:
            async with self._extraction_slots:
                downloaded = await to_thread.run_sync(
                    partial(
                        self._storage.download_file,
                        temporary_path,
                        bucket=document.bucket,
                        key=document.object_key,
                        max_bytes=self._settings.max_source_bytes,
                    )
                )
                if downloaded.size_bytes != document.size_bytes:
                    raise PermanentProcessingError(
                        "Downloaded object size does not match the event"
                    )
                if downloaded.sha256 != document.sha256:
                    raise PermanentProcessingError(
                        "Downloaded object hash does not match the event"
                    )

                await self._set_phase(claimed_run.id, lease_token, RUN_EXTRACTING)
                extraction_media_type = document.detected_media_type
                if document.detected_media_type == PPTX_MEDIA_TYPE:
                    try:
                        extraction_path = await to_thread.run_sync(
                            partial(conversion.convert_to_pdf, temporary_path)
                        )
                    except conversion.ConversionUnavailableError as exc:
                        raise PermanentProcessingError(
                            "This worker cannot convert presentations: no LibreOffice "
                            "is installed in the image"
                        ) from exc
                    except conversion.ConversionError as exc:
                        raise PermanentProcessingError(
                            f"The presentation could not be converted to PDF: {exc}"
                        ) from exc
                    extraction_media_type = PDF_MEDIA_TYPE
                try:
                    extraction = await self._extract_with_heartbeat(
                        claimed_run.id,
                        lease_token,
                        extraction_path,
                        media_type=extraction_media_type,
                    )
                except ExtractionResourceError as exc:
                    raise PermanentProcessingError(
                        f"Extraction ran out of memory: {exc}. Raise "
                        f"RETRIV_PARSER_MEMORY_LIMIT_BYTES (currently "
                        f"{self._settings.parser_memory_limit_bytes / 1024**3:.1f} GiB) "
                        f"and requeue this document."
                    ) from exc
            await self._ensure_active(claimed_run.id, lease_token)
            extraction = await self._describe_images(claimed_run.id, lease_token, extraction)
            await self._ensure_active(claimed_run.id, lease_token)
            self._validate_extraction(extraction)

            artifact_prefix = (
                f"workspaces/{document.origin_workspace_id}/documents/{document.id}/"
                f"runs/{claimed_run.id}/attempts/{lease_token}"
            )
            artifact_key = f"{artifact_prefix}/document.json.gz"
            manifest_key = f"{artifact_prefix}/manifest.json"
            compressed_document = gzip.compress(extraction.document_json, mtime=0)
            artifact_sha256 = hashlib.sha256(compressed_document).hexdigest()
            manifest = self._build_manifest(
                run=claimed_run,
                document=document,
                extraction=extraction,
                artifact_key=artifact_key,
                artifact_sha256=artifact_sha256,
            )
            manifest_bytes = json.dumps(
                manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()

            await self._set_artifact_keys(
                claimed_run.id,
                lease_token,
                artifact_key=artifact_key,
                manifest_key=manifest_key,
            )
            await self._set_phase(claimed_run.id, lease_token, RUN_STORING_ARTIFACT)
            await to_thread.run_sync(
                partial(
                    self._storage.put_bytes,
                    compressed_document,
                    bucket=self._settings.s3_artifact_bucket,
                    key=artifact_key,
                    content_type="application/json",
                    content_encoding="gzip",
                    sha256=artifact_sha256,
                )
            )
            await to_thread.run_sync(
                partial(
                    self._storage.put_bytes,
                    manifest_bytes,
                    bucket=self._settings.s3_artifact_bucket,
                    key=manifest_key,
                    content_type="application/json",
                    content_encoding=None,
                    sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                )
            )
            stored_images: list[DocumentImage] = []
            if extraction.images:
                await self._set_phase(claimed_run.id, lease_token, RUN_STORING_ARTIFACT)
                stored_images = await self._store_images(
                    claimed_run.id,
                    document,
                    extraction.images,
                    extraction.image_descriptions or {},
                    artifact_prefix,
                )

            await self._set_phase(claimed_run.id, lease_token, RUN_CHUNKING)
            chunks_persisted = await self._persist_chunks(
                claimed_run.id,
                lease_token,
                document,
                extraction,
                artifact_key=artifact_key,
                manifest_key=manifest_key,
                stored_images=stored_images,
            )
            if not chunks_persisted:
                await self._delete_artifacts(artifact_key, manifest_key)
        except Exception:
            if artifact_key is not None and manifest_key is not None:
                await self._delete_artifacts(artifact_key, manifest_key)
            raise
        finally:
            await to_thread.run_sync(partial(temporary_path.unlink, missing_ok=True))
            if extraction_path != temporary_path:
                await to_thread.run_sync(
                    partial(shutil.rmtree, extraction_path.parent, ignore_errors=True)
                )
        if chunks_persisted:
            await self._process_embeddings(claimed_run, lease_token)

    async def _store_images(
        self,
        run_id: UUID,
        document: Document,
        images: list[ExtractedImage],
        descriptions: dict[str, str],
        artifact_prefix: str,
    ) -> list[DocumentImage]:
        """Ship a document's pictures to the artifact bucket, sized for a model.

        One object per picture: a render bounded to the edge a vision model
        reads at. A model downscales past that edge itself, so the larger
        render buys nothing -- and when a picture cannot be re-read at all,
        its own bytes are stored as they came rather than failing the run.
        The rows returned here are written inside the run's committing
        transaction, so a crashed attempt leaves objects no database row
        points at and the folder sweep reclaims them.
        """
        stored: list[DocumentImage] = []
        for image in images:
            data, media_type = self._preview_png(image.data, image.media_type)
            object_key = image_object_keys(image.ref, media_type, artifact_prefix)
            await to_thread.run_sync(
                partial(
                    self._storage.put_bytes,
                    data,
                    bucket=self._settings.s3_artifact_bucket,
                    key=object_key,
                    content_type=media_type,
                    content_encoding=None,
                    sha256=hashlib.sha256(data).hexdigest(),
                )
            )
            description = descriptions.get(image.ref, "")
            stored.append(
                DocumentImage(
                    origin_workspace_id=document.origin_workspace_id,
                    document_id=document.id,
                    processing_run_id=run_id,
                    ref=image.ref,
                    bucket=self._settings.s3_artifact_bucket,
                    object_key=object_key,
                    media_type=media_type,
                    size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    page_number=image.page_number,
                    describe_hint=image.describe_hint,
                    caption=image.caption,
                    description=description,
                    described=bool(description),
                )
            )
        return stored

    def _preview_png(self, data: bytes, media_type: str) -> tuple[bytes, str]:
        """The picture, shrunk to the edge a vision model reads at.

        The format is carried over rather than normalised. A JPEG stays a
        JPEG because storing photographic pixels losslessly multiplies the
        file -- a 150 KB photo grows past a megabyte as PNG, since every
        gradient the camera compressed cheaply is then stored exactly. A PNG
        stays a PNG, because that is what charts and screenshots arrive as
        and a lossy re-encode would blur the small labels.

        The shrink target comes from the file header rather than from
        pymupdf, whose page size is reported in DPI-scaled points. A picture
        that cannot be decoded keeps its own bytes: a bad file must not fail
        a run at the artifact stage.
        """
        import pymupdf

        image_format = _image_format_of(data)
        if image_format is None:
            return data, media_type
        size = _image_pixel_size(data, image_format)
        if size is None:
            return data, media_type
        long_edge = max(size)
        if long_edge <= _IMAGE_PREVIEW_EDGE:
            # Already inside the edge: send the picture untouched. Decoding and
            # re-encoding only loses quality (a second lossy generation) or
            # grows the file (a PNG re-packed by a different encoder), and there
            # is nothing to gain because no pixels need discarding.
            return data, media_type
        factor = _IMAGE_PREVIEW_EDGE / long_edge

        try:
            source = pymupdf.open(stream=data)
        except Exception:
            return data, media_type
        try:
            page = source[0]
            # get_pixmap scales the bitmap by the DPI in its metadata, so a
            # plain call is not the header's pixel count. The scale is written
            # against page points to land on the pixel target exactly:
            # scale = (header_pixels * factor) / page_points.
            scale_x = size[0] * factor / max(page.rect.width, 1e-6)
            scale_y = size[1] * factor / max(page.rect.height, 1e-6)
            rendered = page.get_pixmap(matrix=pymupdf.Matrix(scale_x, scale_y), alpha=False)
            if image_format in _LOSSY_IMAGE_FORMATS:
                preview = rendered.tobytes("jpeg", jpg_quality=_PREVIEW_JPEG_QUALITY)
                preview_media_type = "image/jpeg"
            else:
                preview = rendered.tobytes("png")
                preview_media_type = "image/png"
            # A shrink that grew the file is not worth its quality loss: a
            # chart of flat colours compresses better at full size than the
            # antialiased edges a resample introduces.
            if len(preview) >= len(data):
                return data, media_type
            return preview, preview_media_type
        except Exception:
            return data, media_type
        finally:
            source.close()

    async def _process_csv_extract(
        self, claimed_run: ProcessingRun, lease_token: UUID, document: Document
    ) -> None:
        """Store a CSV as queryable rows instead of embedded chunks.

        This deliberately does not go through ``_extract_document``. The
        extraction contract carries a serialized parser artifact plus
        chunk text, and neither is wanted here: rows go to
        ``document_rows`` and Qdrant stays empty for this document. Handing
        100k rows through a JSON contract to then discard them would cost
        more than the parse itself.

        The parser is the stdlib csv module, which is why it runs in-process
        rather than in the extraction sandbox. The sandbox exists because the
        document extractors load native parsing libraries; plain delimited
        text has no such surface, and the caps in the validator bound it
        instead.

        The caller holds a parser slot for the whole call, not just the
        parse: the decoded text stays in memory until the rows are
        committed, so releasing the slot at the parse would let the next
        CSV in while this one still holds its full size in RAM.
        """
        if document.bucket != self._settings.s3_raw_bucket:
            raise PermanentProcessingError("Document references an unapproved raw bucket")
        expected_key = (
            f"workspaces/{document.origin_workspace_id}/objects/{document.sha256}/{document.id}"
        )
        if document.object_key != expected_key:
            raise PermanentProcessingError("Object key does not match document identity")
        if document.size_bytes > self._settings.max_source_bytes:
            raise PermanentProcessingError("Document exceeds the processing size limit")

        self._settings.processing_temp_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="context-source-",
            suffix=".csv",
            dir=self._settings.processing_temp_dir,
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        manifest_key: str | None = None
        rows_persisted = False
        try:
            downloaded = await to_thread.run_sync(
                partial(
                    self._storage.download_file,
                    temporary_path,
                    bucket=document.bucket,
                    key=document.object_key,
                    max_bytes=self._settings.max_source_bytes,
                )
            )
            if downloaded.size_bytes != document.size_bytes:
                raise PermanentProcessingError("Downloaded object size does not match the event")
            if downloaded.sha256 != document.sha256:
                raise PermanentProcessingError("Downloaded object hash does not match the event")

            await self._set_phase(claimed_run.id, lease_token, RUN_EXTRACTING)
            csv_text = await to_thread.run_sync(partial(self._decode_csv, temporary_path))
            columns, delimiter = await to_thread.run_sync(partial(self._inspect_csv, csv_text))

            artifact_prefix = (
                f"workspaces/{document.origin_workspace_id}/documents/{document.id}/"
                f"runs/{claimed_run.id}/attempts/{lease_token}"
            )
            manifest_key = f"{artifact_prefix}/manifest.json"
            manifest_bytes = json.dumps(
                {
                    "columns": columns,
                    "delimiter": delimiter,
                    "encoding": CSV_ENCODINGS[0],
                    "media_type": document.detected_media_type,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
            await self._set_artifact_keys(
                claimed_run.id,
                lease_token,
                artifact_key=None,
                manifest_key=manifest_key,
            )
            await self._set_phase(claimed_run.id, lease_token, RUN_STORING_ARTIFACT)
            await to_thread.run_sync(
                partial(
                    self._storage.put_bytes,
                    manifest_bytes,
                    bucket=self._settings.s3_artifact_bucket,
                    key=manifest_key,
                    content_type="application/json",
                    content_encoding=None,
                    sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                )
            )

            await self._set_phase(claimed_run.id, lease_token, RUN_CHUNKING)
            rows_persisted = await self._persist_tabular_rows(
                claimed_run.id,
                lease_token,
                document,
                [
                    SheetTable(
                        name="",
                        columns=columns,
                        header_row=1,
                        rows=self._iter_csv_rows(csv_text, columns, delimiter),
                    )
                ],
                manifest_key=manifest_key,
                stored_images=[],
            )
            if not rows_persisted:
                await self._delete_artifacts(None, manifest_key)
            else:
                await self._sweep_superseded_artifacts(document.id, claimed_run.id)
        except Exception:
            if manifest_key is not None:
                await self._delete_artifacts(None, manifest_key)
            raise
        finally:
            await to_thread.run_sync(partial(temporary_path.unlink, missing_ok=True))

    async def _process_xlsx_extract(
        self, claimed_run: ProcessingRun, lease_token: UUID, document: Document
    ) -> None:
        """Store a workbook's sheets as rows, and its pictures for the agent.

        The data path is the CSV path with names: every tabular sheet lands
        in ``document_rows`` under its sheet name, and Qdrant stays empty
        for the document. What a CSV cannot carry -- embedded pictures,
        charts -- is lifted here and described by the vision stage, so a
        workbook's graphs are as reachable as its numbers: each description
        is stored beside its picture and served through the image tools.

        The parser runs in-process next to the CSV parser: openpyxl is pure
        Python and holds no native parsing surface, and the caps bound the
        work instead of a sandbox.
        """
        if document.bucket != self._settings.s3_raw_bucket:
            raise PermanentProcessingError("Document references an unapproved raw bucket")
        expected_key = (
            f"workspaces/{document.origin_workspace_id}/objects/{document.sha256}/{document.id}"
        )
        if document.object_key != expected_key:
            raise PermanentProcessingError("Object key does not match document identity")
        if document.size_bytes > self._settings.max_source_bytes:
            raise PermanentProcessingError("Document exceeds the processing size limit")

        self._settings.processing_temp_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="context-source-",
            suffix=".xlsx",
            dir=self._settings.processing_temp_dir,
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        manifest_key: str | None = None
        try:
            downloaded = await to_thread.run_sync(
                partial(
                    self._storage.download_file,
                    temporary_path,
                    bucket=document.bucket,
                    key=document.object_key,
                    max_bytes=self._settings.max_source_bytes,
                )
            )
            if downloaded.size_bytes != document.size_bytes:
                raise PermanentProcessingError("Downloaded object size does not match the event")
            if downloaded.sha256 != document.sha256:
                raise PermanentProcessingError("Downloaded object hash does not match the event")

            await self._set_phase(claimed_run.id, lease_token, RUN_EXTRACTING)
            try:
                reading = await to_thread.run_sync(
                    partial(
                        read_workbook,
                        temporary_path,
                        max_sheets=self._settings.max_xlsx_sheets,
                        max_rows=self._settings.max_csv_rows,
                        max_columns=self._settings.max_csv_columns,
                        max_images=self._settings.max_xlsx_images,
                    )
                )
            except WorkbookTooLargeError as exc:
                raise PermanentProcessingError(str(exc)) from exc
            except WorkbookError as exc:
                raise PermanentProcessingError(
                    f"The workbook could not be read: {exc}"
                ) from exc

            artifact_prefix = (
                f"workspaces/{document.origin_workspace_id}/documents/{document.id}/"
                f"runs/{claimed_run.id}/attempts/{lease_token}"
            )
            manifest_key = f"{artifact_prefix}/manifest.json"
            manifest_bytes = json.dumps(
                {
                    "artifact_type": "retriv-spreadsheet",
                    "sheets": [
                        {
                            "sheet": table.name,
                            "columns": table.columns,
                            "header_row": table.header_row,
                        }
                        for table in reading.tables
                    ],
                    "skipped": [
                        {"sheet": sheet.name, "reason": sheet.reason}
                        for sheet in reading.skipped
                    ],
                    "visuals_skipped": reading.visuals_skipped,
                    "media_type": document.detected_media_type,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
            await self._set_artifact_keys(
                claimed_run.id,
                lease_token,
                artifact_key=None,
                manifest_key=manifest_key,
            )
            await self._set_phase(claimed_run.id, lease_token, RUN_STORING_ARTIFACT)
            await to_thread.run_sync(
                partial(
                    self._storage.put_bytes,
                    manifest_bytes,
                    bucket=self._settings.s3_artifact_bucket,
                    key=manifest_key,
                    content_type="application/json",
                    content_encoding=None,
                    sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                )
            )

            descriptions: dict[str, str] = {}
            if reading.pictures and self._settings.image_processing_enabled:
                descriptions = await self._describe_xlsx_images(
                    claimed_run.id, lease_token, reading.pictures
                )
            await self._ensure_active(claimed_run.id, lease_token)

            stored_images = await self._store_images(
                claimed_run.id,
                document,
                [picture.image for picture in reading.pictures],
                descriptions,
                artifact_prefix,
            )

            await self._set_phase(claimed_run.id, lease_token, RUN_CHUNKING)
            rows_persisted = await self._persist_tabular_rows(
                claimed_run.id,
                lease_token,
                document,
                reading.tables,
                manifest_key=manifest_key,
                stored_images=stored_images,
            )
            if not rows_persisted:
                await self._delete_artifacts(None, manifest_key)
            else:
                await self._sweep_superseded_artifacts(document.id, claimed_run.id)
        except Exception:
            if manifest_key is not None:
                await self._delete_artifacts(None, manifest_key)
            raise
        finally:
            await to_thread.run_sync(partial(temporary_path.unlink, missing_ok=True))

    async def _describe_xlsx_images(
        self, run_id: UUID, lease_token: UUID, pictures: list[SheetPicture]
    ) -> dict[str, str]:
        """Describe a workbook's pictures and charts, a few at a time.

        The concurrency cap and the failure posture are the document path's:
        the budget belongs to the worker, not to a run, and a description
        that cannot be produced fails the run rather than shipping a
        picture nobody can read about.
        """
        provider = self._vision_provider
        if provider is None:
            raise PermanentProcessingError(
                "Image processing is enabled but no vision provider is configured"
            )
        await self._set_phase(run_id, lease_token, RUN_DESCRIBING)
        stop_event = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat_lease(run_id, lease_token, stop_event))

        async def describe_one(picture: SheetPicture) -> str:
            async with self._vlm_slots:
                context = (
                    f'The picture appears on the sheet "{picture.sheet}" '
                    "of a spreadsheet workbook.\n"
                )
                if picture.image.caption.strip():
                    context += f"The chart is titled: {picture.image.caption.strip()}\n"
                return await provider.describe(picture.image, context=context)

        try:
            with _tracer.start_as_current_span(
                "describe", attributes={"retriv.image_count": len(pictures)}
            ) as span:
                try:
                    async with asyncio.timeout(self._settings.processing_timeout_seconds):
                        results = await asyncio.gather(
                            *(describe_one(picture) for picture in pictures),
                            return_exceptions=True,
                        )
                except TimeoutError as exc:
                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    raise ExtractionError(
                        "Describing the workbook's pictures exceeded the time limit"
                    ) from exc
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            return {
                picture.image.ref: str(description)
                for picture, description in zip(pictures, results, strict=True)
            }
        finally:
            stop_event.set()
            await heartbeat

    def _decode_csv(self, path: Path) -> str:
        """Decode the file to text using the same encodings the validator tried.

        Running the decode separately keeps the file read off the event loop,
        and sharing ``CSV_ENCODINGS`` keeps the worker from disagreeing with
        validation about what the file contains.
        """
        raw = path.read_bytes()
        for encoding in CSV_ENCODINGS:
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise PermanentProcessingError("CSV could not be decoded as UTF-8 text")

    def _inspect_csv(self, text: str) -> tuple[list[str], str]:
        """Return the uniquified header and the delimiter both sides agreed on.

        Reusing ``sniff_delimiter`` matters: if the worker sniffed a
        different separator than the validator did, a file that passed the
        field-count check would fail here with a row-count error naming
        neither cause.
        """
        delimiter = sniff_delimiter(text)
        try:
            header = next(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
        except (StopIteration, csv.Error) as exc:
            raise PermanentProcessingError("CSV header could not be read") from exc
        return uniquify_columns([name.strip() for name in header]), delimiter

    def _iter_csv_rows(
        self, text: str, columns: list[str], delimiter: str
    ) -> Iterator[dict[str, str]]:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        next(reader, None)
        for row in reader:
            if not row:
                continue
            yield dict(zip(columns, row, strict=False))

    async def _persist_tabular_rows(
        self,
        run_id: UUID,
        lease_token: UUID,
        document: Document,
        tables: list[SheetTable],
        *,
        manifest_key: str,
        stored_images: list[DocumentImage],
    ) -> bool:
        """Store one tabular document's rows, replacing whatever it had.

        A CSV arrives as one unnamed table and a workbook as several named
        ones; both land here, because the store, the caps and the citation
        contract are the same. The row id seeds from the sheet name when
        there is one, and keeps the older seed for a CSV so existing rows
        keep their identity. This is also the run's activation gate: the
        document row is locked, so a delete that committed meanwhile turns
        the run into a cancellation instead of a write over a tombstone.
        """
        stop_event = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat_lease(run_id, lease_token, stop_event))
        try:
            now = utcnow()
            async with self._session_factory() as session, session.begin():
                run = await session.get(ProcessingRun, run_id)
                if run is None or run.lease_token != lease_token:
                    raise LeaseLostError()
                stored_document = await session.scalar(
                    select(Document).where(Document.id == document.id).with_for_update()
                )
                if stored_document is None:
                    raise PermanentProcessingError("Processing document disappeared")
                if stored_document.deleted_at is not None or run.status == RUN_CANCELLED:
                    run.status = RUN_CANCELLED
                    run.lease_until = None
                    run.lease_token = None
                    run.completed_at = now
                    return False

                await session.execute(
                    delete(DocumentRow).where(DocumentRow.processing_run_id == run_id)
                )
                inserted_total = 0
                catalogue: list[dict[str, object]] = []
                for table in tables:
                    inserted = 0
                    batch: list[dict[str, object]] = []
                    for row_number, row in enumerate(table.rows, start=1):
                        seed = str(row_number) if not table.name else f"{table.name}:{row_number}"
                        batch.append(
                            {
                                "id": uuid5(run_id, seed),
                                "origin_workspace_id": stored_document.origin_workspace_id,
                                "document_id": stored_document.id,
                                "processing_run_id": run_id,
                                "sheet": table.name,
                                "row_number": row_number,
                                "data": row,
                            }
                        )
                        if len(batch) >= _ROW_INSERT_BATCH:
                            await session.execute(insert(DocumentRow), batch)
                            inserted += len(batch)
                            batch.clear()
                            await asyncio.sleep(0)
                    if batch:
                        await session.execute(insert(DocumentRow), batch)
                        inserted += len(batch)
                    inserted_total += inserted
                    catalogue.append(
                        {"sheet": table.name, "columns": table.columns, "rows": inserted}
                    )

                await session.execute(
                    delete(DocumentRow).where(
                        DocumentRow.document_id == stored_document.id,
                        DocumentRow.processing_run_id != run_id,
                    )
                )
                await session.execute(
                    delete(DocumentImage).where(
                        DocumentImage.document_id == stored_document.id
                    )
                )
                session.add_all(stored_images)

                run.status = RUN_READY
                run.lease_until = None
                run.lease_token = None
                run.artifact_bucket = self._settings.s3_artifact_bucket
                run.artifact_key = None
                run.manifest_key = manifest_key
                run.chunk_count = 0
                run.embedded_chunk_count = 0
                run.row_count = inserted_total
                run.csv_columns = catalogue
                run.embedding_profile = None
                run.embedding_model = None
                run.embedding_dimensions = None
                run.completed_at = now
                stored_document.active_processing_run_id = run_id
                return True
        finally:
            stop_event.set()
            await heartbeat

    async def _document_workspace_ids(self, document_id: UUID) -> list[str]:
        """The workspace links a document currently has, for the Qdrant payload."""
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(DocumentWorkspace.workspace_id)
                .where(
                    DocumentWorkspace.document_id == document_id,
                    DocumentWorkspace.deleted_at.is_(None),
                )
                .order_by(DocumentWorkspace.workspace_id)
            )
            return sorted({str(workspace_id) for workspace_id in rows.all()})

    async def _process_access_sync(self, claimed_run: ProcessingRun) -> None:
        """Refresh the Qdrant workspace array after a link changed.

        One payload update per document covers every chunk. No chunk is read
        and no vector is touched.
        """
        lease_token = self._require_lease_token(claimed_run)
        document = await self._load_document(claimed_run)
        if document.deleted_at is not None:
            await self._cancel_run(claimed_run.id, lease_token)
            return
        workspace_ids = await self._document_workspace_ids(document.id)
        await self._vector_store.set_document_workspaces(
            document.id, [UUID(value) for value in workspace_ids]
        )
        async with self._session_factory() as session, session.begin():
            run = await session.get(ProcessingRun, claimed_run.id)
            if run is None or run.lease_token != lease_token:
                raise LeaseLostError()
            run.status = RUN_READY
            run.lease_until = None
            run.lease_token = None
            run.completed_at = utcnow()
            run.last_error = None

    async def _process_delete(self, claimed_run: ProcessingRun) -> None:
        """Remove one document from every store it occupies.

        The vectors go first, then the artifact folder, then the raw
        upload, and the row that holds the raw object's address goes last:
        a failure once the address is gone could not be retried with the
        key in hand. The raw delete lives here rather than on the API
        route because a failed attempt lands back in the queue, while the
        route's caller is long gone.
        """
        lease_token = self._require_lease_token(claimed_run)
        await self._vector_store.delete_document(claimed_run.document_id)
        artifact_prefix = (
            f"workspaces/{claimed_run.origin_workspace_id}/documents/{claimed_run.document_id}/"
        )
        await to_thread.run_sync(
            partial(
                self._storage.delete_prefix,
                bucket=self._settings.s3_artifact_bucket,
                prefix=artifact_prefix,
            )
        )
        raw_bucket, raw_key = await self._raw_object_location(claimed_run.document_id)
        if raw_key is not None:
            await to_thread.run_sync(
                partial(
                    self._storage.delete_object,
                    bucket=raw_bucket or self._settings.s3_raw_bucket,
                    key=raw_key,
                )
            )
        await self._set_phase(claimed_run.id, lease_token, RUN_DELETING)

        async with self._session_factory() as session, session.begin():
            run = await session.get(ProcessingRun, claimed_run.id)
            if run is None or run.lease_token != lease_token:
                raise LeaseLostError()
            document = await session.scalar(
                select(Document).where(Document.id == claimed_run.document_id).with_for_update()
            )
            if document is not None:
                await session.delete(document)

    async def _raw_object_location(self, document_id: UUID) -> tuple[str | None, str | None]:
        """The bucket and key of a document's original upload.

        The document row is the only record of that address, and this run
        ends by deleting the row, so the location is read before anything
        irreversible happens. ``None`` means the row is already gone, which
        a retry after a completed attempt can hit; the object it named was
        deleted then too, and the delete call is a no-op on a missing key.
        """
        async with self._session_factory() as session:
            row = await session.execute(
                select(Document.bucket, Document.object_key).where(Document.id == document_id)
            )
            location = row.first()
        if location is None:
            return None, None
        return location[0], location[1]

    async def _load_document(self, run: ProcessingRun) -> Document:
        async with self._session_factory() as session:
            document = await session.get(Document, run.document_id)
            if document is None:
                raise PermanentProcessingError("Source document metadata is missing")
            return document

    def _validate_extraction(self, extraction: ExtractionResult) -> None:
        if not extraction.document_json:
            raise PermanentProcessingError("Extractor returned an empty document artifact")
        if not extraction.chunks:
            raise PermanentProcessingError("Extractor returned no document chunks")
        if len(extraction.chunks) > self._settings.max_chunks_per_document:
            raise PermanentProcessingError("Document produced too many chunks")
        if any(not chunk.text.strip() for chunk in extraction.chunks):
            raise PermanentProcessingError("Extractor returned an empty chunk")
        if len(extraction.document_json) > self._settings.max_artifact_bytes:
            raise PermanentProcessingError("Document artifact exceeds the processing limit")
        extracted_text_bytes = sum(len(chunk.text.encode("utf-8")) for chunk in extraction.chunks)
        if extracted_text_bytes > self._settings.max_extracted_text_bytes:
            raise PermanentProcessingError("Extracted text exceeds the processing limit")
        self._validate_extraction_coverage(extraction)

    def _validate_extraction_coverage(self, extraction: ExtractionResult) -> None:
        """Reject an extraction that kept far less text than the file actually holds.

        Compares chunk text against the source file's own text layer, which is read
        independently of the parser. Without this, a parser regression that silently
        drops half a document still reports success and gets indexed: the vector store
        ends up with an arbitrary subset and nothing downstream can tell, because a
        half-parsed document is indistinguishable from a short one.
        """
        minimum = self._settings.min_extraction_coverage
        source_chars = extraction.source_text_chars
        if source_chars <= 0:
            return
        extracted_chars = sum(len(chunk.text) for chunk in extraction.chunks)
        ratio = extracted_chars / source_chars
        if ratio < minimum:
            pages = {
                page
                for chunk in extraction.chunks
                for page in chunk.page_numbers
                if isinstance(page, int)
            }
            logger.error(
                "Extraction coverage below minimum: extracted=%d source=%d ratio=%.4f "
                "chunks=%d pages_covered=%d pages_total=%d first_page=%s last_page=%s "
                "ocr=%s extractor=%s",
                extracted_chars,
                source_chars,
                ratio,
                len(extraction.chunks),
                len(pages),
                extraction.page_count,
                min(pages) if pages else None,
                max(pages) if pages else None,
                extraction.ocr_enabled,
                extraction.extractor_version,
            )
            raise PermanentProcessingError(
                f"Extraction kept only {ratio:.0%} of the text in this document "
                f"({extracted_chars} of {source_chars} characters, "
                f"{len(pages)} of {extraction.page_count or 'unknown'} pages); "
                f"at least {minimum:.0%} is required"
            )

    def _build_manifest(
        self,
        *,
        run: ProcessingRun,
        document: Document,
        extraction: ExtractionResult,
        artifact_key: str,
        artifact_sha256: str,
    ) -> dict[str, object]:
        return {
            "artifact_type": "retriv-markdown",
            "artifact_schema_version": 2,
            "workspace_id": str(document.origin_workspace_id),
            "document_id": str(document.id),
            "run_id": str(run.id),
            "source": {
                "bucket": document.bucket,
                "object_key": document.object_key,
                "sha256": document.sha256,
                "filename": document.original_filename,
                "media_type": document.detected_media_type,
                "size_bytes": document.size_bytes,
            },
            "pipeline": {
                "name": "retriv-context-pipeline",
                "version": self._settings.pipeline_version,
                "extractor_version": extraction.extractor_version,
                "ocr_enabled": extraction.ocr_enabled,
                "chunk_max_tokens": self._settings.chunk_max_tokens,
                "source_text_chars": extraction.source_text_chars,
                "extracted_text_chars": sum(len(chunk.text) for chunk in extraction.chunks),
            },
            "artifact": {
                "bucket": self._settings.s3_artifact_bucket,
                "key": artifact_key,
                "content_type": "application/json",
                "content_encoding": "gzip",
                "sha256": artifact_sha256,
            },
            "vision": {
                "enabled": self._settings.image_processing_enabled,
                "model": self._settings.vlm_model,
                "images_found": len(extraction.images),
                "images_described": self._count_described(extraction.chunks),
            },
            "chunk_count": len(extraction.chunks),
        }

    @staticmethod
    def _count_described(chunks: list[ExtractedChunk]) -> int:
        """How many pictures ended up with a description."""
        return sum(
            1
            for chunk in chunks
            for entry in (chunk.provenance.get("images") or [])
            if isinstance(entry, dict) and entry.get("described")
        )

    async def _process_embeddings(self, claimed_run: ProcessingRun, lease_token: UUID) -> None:
        stop_event = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat_lease(claimed_run.id, lease_token, stop_event)
        )
        try:
            with _tracer.start_as_current_span("embed") as span:
                await self._vector_store.ensure_collection()
                embedded = 0
                while True:
                    batch = await self._load_embedding_batch(claimed_run.id)
                    if not batch:
                        break
                    await self._mark_embedding_batch_processing(
                        claimed_run.id,
                        lease_token,
                        [embedding.chunk_id for _, embedding in batch],
                    )
                    vectors = await self._embedding_provider.embed(
                        [chunk.text for chunk, _ in batch]
                    )
                    workspace_ids = await self._document_workspace_ids(claimed_run.document_id)
                    if len(vectors) != len(batch):
                        raise EmbeddingProviderError(
                            "Embedding response count does not match the request",
                            retryable=False,
                        )
                    points = [
                        VectorPoint(
                            id=embedding.qdrant_point_id,
                            vector=vector,
                            payload={
                                "origin_workspace_id": str(chunk.origin_workspace_id),
                                "workspace_ids": workspace_ids,
                                "document_id": str(chunk.document_id),
                                "processing_run_id": str(chunk.processing_run_id),
                                "chunk_id": str(chunk.id),
                                "content_hash": chunk.content_hash,
                                "embedding_profile": self._settings.embedding_profile,
                                "embedding_model": self._settings.embedding_model,
                                "dimensions": self._settings.embedding_dimensions,
                                "ordinal": chunk.ordinal,
                            },
                        )
                        for (chunk, embedding), vector in zip(batch, vectors, strict=True)
                    ]
                    await self._vector_store.upsert(points)
                    try:
                        await self._mark_embedding_batch_indexed(
                            claimed_run.id,
                            lease_token,
                            [embedding.chunk_id for _, embedding in batch],
                        )
                    except LeaseLostError:
                        with suppress(VectorStoreError):
                            await self._vector_store.delete_points([point.id for point in points])
                        raise
                    embedded += len(batch)
                span.set_attribute("retriv.chunk_count", embedded)
            activated = await self._activate_embedded_run(claimed_run.id, lease_token)
            if not activated:
                await self._vector_store.delete_document(claimed_run.document_id)
            else:
                await self._vector_store.set_document_workspaces(
                    claimed_run.document_id,
                    [
                        UUID(value)
                        for value in await self._document_workspace_ids(claimed_run.document_id)
                    ],
                    processing_run_id=claimed_run.id,
                )
        finally:
            stop_event.set()
            await heartbeat

    async def _load_embedding_batch(
        self, run_id: UUID
    ) -> list[tuple[DocumentChunk, ChunkEmbedding]]:
        async with self._session_factory() as session:
            rows = await session.execute(
                select(DocumentChunk, ChunkEmbedding)
                .join(
                    ChunkEmbedding,
                    and_(
                        ChunkEmbedding.chunk_id == DocumentChunk.id,
                        ChunkEmbedding.embedding_profile == self._settings.embedding_profile,
                    ),
                )
                .where(
                    DocumentChunk.processing_run_id == run_id,
                    ChunkEmbedding.status != EMBEDDING_INDEXED,
                )
                .order_by(DocumentChunk.ordinal)
                .limit(self._settings.embedding_batch_size)
            )
            return [(chunk, embedding) for chunk, embedding in rows.all()]

    async def _mark_embedding_batch_processing(
        self, run_id: UUID, lease_token: UUID, chunk_ids: list[UUID]
    ) -> None:
        now = utcnow()
        async with self._session_factory() as session, session.begin():
            run = await session.get(ProcessingRun, run_id)
            if run is None or run.lease_token != lease_token or run.status != RUN_EMBEDDING:
                raise LeaseLostError()
            result = await session.execute(
                update(ChunkEmbedding)
                .where(
                    ChunkEmbedding.processing_run_id == run_id,
                    ChunkEmbedding.chunk_id.in_(chunk_ids),
                    ChunkEmbedding.embedding_profile == self._settings.embedding_profile,
                    ChunkEmbedding.status != EMBEDDING_INDEXED,
                )
                .values(
                    status=EMBEDDING_PROCESSING,
                    attempts=ChunkEmbedding.attempts + 1,
                    last_error=None,
                    updated_at=now,
                )
            )
            if getattr(result, "rowcount", 0) != len(chunk_ids):
                raise LeaseLostError()

    async def _mark_embedding_batch_indexed(
        self, run_id: UUID, lease_token: UUID, chunk_ids: list[UUID]
    ) -> None:
        now = utcnow()
        async with self._session_factory() as session, session.begin():
            run = await session.get(ProcessingRun, run_id)
            if run is None or run.lease_token != lease_token or run.status != RUN_EMBEDDING:
                raise LeaseLostError()
            document = await session.get(Document, run.document_id)
            if document is None or document.deleted_at is not None:
                raise LeaseLostError()
            result = await session.execute(
                update(ChunkEmbedding)
                .where(
                    ChunkEmbedding.processing_run_id == run_id,
                    ChunkEmbedding.chunk_id.in_(chunk_ids),
                    ChunkEmbedding.embedding_profile == self._settings.embedding_profile,
                    ChunkEmbedding.status == EMBEDDING_PROCESSING,
                )
                .values(
                    status=EMBEDDING_INDEXED,
                    last_error=None,
                    embedded_at=now,
                    updated_at=now,
                )
            )
            if getattr(result, "rowcount", 0) != len(chunk_ids):
                raise LeaseLostError()
            run.embedded_chunk_count = (run.embedded_chunk_count or 0) + len(chunk_ids)

    async def _activate_embedded_run(self, run_id: UUID, lease_token: UUID) -> bool:
        now = utcnow()
        document_id: UUID | None = None
        async with self._session_factory() as session, session.begin():
            run = await session.get(ProcessingRun, run_id)
            if run is None or run.lease_token != lease_token:
                raise LeaseLostError()
            document = await session.scalar(
                select(Document).where(Document.id == run.document_id).with_for_update()
            )
            if document is None:
                raise PermanentProcessingError("Processing document disappeared")
            if document.deleted_at is not None or run.status == RUN_CANCELLED:
                run.status = RUN_CANCELLED
                run.lease_until = None
                run.lease_token = None
                run.completed_at = now
                return False
            chunk_count = await session.scalar(
                select(func.count())
                .select_from(DocumentChunk)
                .where(DocumentChunk.processing_run_id == run_id)
            )
            indexed_count = await session.scalar(
                select(func.count())
                .select_from(ChunkEmbedding)
                .join(DocumentChunk, ChunkEmbedding.chunk_id == DocumentChunk.id)
                .where(
                    DocumentChunk.processing_run_id == run_id,
                    ChunkEmbedding.embedding_profile == self._settings.embedding_profile,
                    ChunkEmbedding.status == EMBEDDING_INDEXED,
                    ChunkEmbedding.model == self._settings.embedding_model,
                    ChunkEmbedding.dimensions == self._settings.embedding_dimensions,
                    ChunkEmbedding.content_hash == DocumentChunk.content_hash,
                )
            )
            if not chunk_count or indexed_count != chunk_count:
                raise PermanentProcessingError(
                    "Not every document chunk has a matching Qdrant embedding"
                )
            run.status = RUN_READY
            run.lease_until = None
            run.lease_token = None
            run.chunk_count = int(chunk_count or 0)
            run.embedded_chunk_count = int(indexed_count or 0)
            run.completed_at = now
            document.active_processing_run_id = run.id
            document_id = run.document_id
        if document_id is not None:
            await self._sweep_superseded_artifacts(document_id, run_id)
            await self._sweep_superseded_vectors(document_id, run_id)
        return True

    async def _sweep_superseded_vectors(self, document_id: UUID, keep_run_id: UUID) -> None:
        """Remove the Qdrant points of runs this run superseded.

        Retrieval validates every vector match against the active processing
        run, so a superseded run's points can never be served -- but they are
        still returned by the vector search, and a search that hits one fails
        as a whole rather than skipping it. Leaving them behind therefore
        makes the document's whole workspace unsearchable until they are gone.

        The rows in PostgreSQL stay as history, exactly as they do for the
        artifacts; only the vectors go.
        """
        async with self._session_factory() as session:
            point_ids = list(
                (
                    await session.scalars(
                        select(ChunkEmbedding.qdrant_point_id).where(
                            ChunkEmbedding.document_id == document_id,
                            ChunkEmbedding.processing_run_id != keep_run_id,
                        )
                    )
                ).all()
            )
        if not point_ids:
            return
        try:
            await self._vector_store.delete_points(point_ids)
            logger.info(
                "Superseded vector sweep removed points",
                extra={
                    "document_id": str(document_id),
                    "kept_run_id": str(keep_run_id),
                    "point_count": len(point_ids),
                },
            )
        except VectorStoreError:
            logger.warning(
                "Superseded vector sweep failed",
                extra={
                    "document_id": str(document_id),
                    "point_count": len(point_ids),
                },
                exc_info=True,
            )

    async def _sweep_superseded_artifacts(self, document_id: UUID, keep_run_id: UUID) -> None:
        """Remove the artifact folders of runs this run superseded.

        Reindexing piles up one folder per run; the winning run's activation
        is the moment the older ones stop mattering. Their database rows
        stay as history -- only the bucket folders go.
        """
        async with self._session_factory() as session:
            keys = (
                await session.execute(
                    select(ProcessingRun.artifact_key, ProcessingRun.manifest_key).where(
                        ProcessingRun.document_id == document_id,
                        ProcessingRun.id != keep_run_id,
                        or_(
                            ProcessingRun.artifact_key.is_not(None),
                            ProcessingRun.manifest_key.is_not(None),
                        ),
                    )
                )
            ).all()
        prefixes = {key.rsplit("/", 1)[0] for row in keys for key in row if key is not None}
        for prefix in prefixes:
            with suppress(StorageError):
                await to_thread.run_sync(
                    partial(
                        self._storage.delete_prefix,
                        bucket=self._settings.s3_artifact_bucket,
                        prefix=f"{prefix}/",
                    )
                )

    async def _persist_chunks(
        self,
        run_id: UUID,
        lease_token: UUID,
        document: Document,
        extraction: ExtractionResult,
        *,
        artifact_key: str,
        manifest_key: str,
        stored_images: list[DocumentImage],
    ) -> bool:
        now = utcnow()
        async with self._session_factory() as session, session.begin():
            stored_document = await session.scalar(
                select(Document).where(Document.id == document.id).with_for_update()
            )
            run = await session.get(ProcessingRun, run_id)
            if stored_document is None or run is None:
                raise PermanentProcessingError("Processing state disappeared")
            if run.lease_token != lease_token:
                raise LeaseLostError()
            if stored_document.deleted_at is not None or run.status == RUN_CANCELLED:
                run.status = RUN_CANCELLED
                run.lease_until = None
                run.lease_token = None
                run.completed_at = now
                return False

            await session.execute(
                delete(ChunkEmbedding).where(ChunkEmbedding.processing_run_id == run_id)
            )
            await session.execute(
                delete(DocumentChunk).where(DocumentChunk.processing_run_id == run_id)
            )
            await session.execute(
                delete(DocumentImage).where(DocumentImage.document_id == document.id)
            )
            session.add_all(stored_images)
            stored_chunks: list[DocumentChunk] = []
            for ordinal, chunk in enumerate(extraction.chunks):
                content_hash = hashlib.sha256(chunk.text.encode()).hexdigest()
                stored_chunk = DocumentChunk(
                    id=uuid5(run_id, f"{ordinal}:{content_hash}"),
                    origin_workspace_id=document.origin_workspace_id,
                    document_id=document.id,
                    processing_run_id=run_id,
                    ordinal=ordinal,
                    text=chunk.text,
                    token_count=chunk.token_count,
                    section_path=chunk.section_path,
                    section_key=chunk.section_key,
                    is_heading_only=chunk.is_heading_only,
                    page_numbers=chunk.page_numbers,
                    provenance=chunk.provenance,
                    content_hash=content_hash,
                )
                session.add(stored_chunk)
                stored_chunks.append(stored_chunk)
            await session.flush()
            session.add_all(self._embedding_record(chunk) for chunk in stored_chunks)
            run.status = RUN_EMBEDDING
            run.artifact_bucket = self._settings.s3_artifact_bucket
            run.artifact_key = artifact_key
            run.manifest_key = manifest_key
            run.chunk_count = len(extraction.chunks)
            run.embedded_chunk_count = 0
            run.embedding_profile = self._settings.embedding_profile
            run.embedding_model = self._settings.embedding_model
            run.embedding_dimensions = self._settings.embedding_dimensions
            run.completed_at = None
            return True

    def _embedding_record(self, chunk: DocumentChunk) -> ChunkEmbedding:
        return self._new_embedding_record(
            chunk_id=chunk.id,
            origin_workspace_id=chunk.origin_workspace_id,
            document_id=chunk.document_id,
            processing_run_id=chunk.processing_run_id,
            content_hash=chunk.content_hash,
        )

    def _new_embedding_record(
        self,
        *,
        chunk_id: UUID,
        origin_workspace_id: UUID,
        document_id: UUID,
        processing_run_id: UUID,
        content_hash: str,
    ) -> ChunkEmbedding:
        return ChunkEmbedding(
            chunk_id=chunk_id,
            embedding_profile=self._settings.embedding_profile,
            origin_workspace_id=origin_workspace_id,
            document_id=document_id,
            processing_run_id=processing_run_id,
            model=self._settings.embedding_model,
            dimensions=self._settings.embedding_dimensions,
            content_hash=content_hash,
            qdrant_point_id=self._qdrant_point_id(chunk_id),
            status=EMBEDDING_PENDING,
        )

    def _qdrant_point_id(self, chunk_id: UUID) -> UUID:
        return uuid5(chunk_id, self._settings.embedding_profile)

    async def _ensure_active(self, run_id: UUID, lease_token: UUID) -> None:
        """Stop work whose run was cancelled or whose lease is gone.

        Deletion cancels a run in the database while the worker is inside a
        stage that cannot be interrupted. Between stages the run is read
        back: a cancelled or lease-less run abandons the rest of the
        pipeline instead of describing and embedding a document that no
        longer exists, and the pipeline slot it held is freed at once.
        """
        async with self._session_factory() as session:
            run = await session.get(ProcessingRun, run_id)
            if run is None or run.lease_token != lease_token or run.status == RUN_CANCELLED:
                raise LeaseLostError()

    async def _set_phase(self, run_id: UUID, lease_token: UUID, status: str) -> None:
        lease_until = utcnow() + timedelta(seconds=self._settings.processing_lease_seconds)
        async with self._session_factory() as session:
            result = await session.execute(
                update(ProcessingRun)
                .where(
                    ProcessingRun.id == run_id,
                    ProcessingRun.lease_token == lease_token,
                    ProcessingRun.status != RUN_CANCELLED,
                )
                .values(status=status, lease_until=lease_until)
            )
            await session.commit()
            if getattr(result, "rowcount", 0) != 1:
                raise LeaseLostError()

    async def _set_artifact_keys(
        self,
        run_id: UUID,
        lease_token: UUID,
        *,
        artifact_key: str | None,
        manifest_key: str,
    ) -> None:
        async with self._session_factory() as session:
            result = await session.execute(
                update(ProcessingRun)
                .where(
                    ProcessingRun.id == run_id,
                    ProcessingRun.lease_token == lease_token,
                    ProcessingRun.status != RUN_CANCELLED,
                )
                .values(
                    artifact_bucket=self._settings.s3_artifact_bucket,
                    artifact_key=artifact_key,
                    manifest_key=manifest_key,
                )
            )
            await session.commit()
            if getattr(result, "rowcount", 0) != 1:
                raise LeaseLostError()

    async def _cancel_run(self, run_id: UUID, lease_token: UUID) -> None:
        async with self._session_factory() as session:
            result = await session.execute(
                update(ProcessingRun)
                .where(
                    ProcessingRun.id == run_id,
                    ProcessingRun.lease_token == lease_token,
                )
                .values(
                    status=RUN_CANCELLED,
                    lease_until=None,
                    lease_token=None,
                    completed_at=utcnow(),
                    last_error="Document was deleted at the source",
                )
            )
            await session.commit()
            if getattr(result, "rowcount", 0) != 1:
                raise LeaseLostError()

    async def _record_failure(
        self, claimed_run: ProcessingRun, error: Exception, *, permanent: bool
    ) -> None:
        lease_token = self._require_lease_token(claimed_run)
        async with self._session_factory() as session, session.begin():
            run = await session.scalar(
                select(ProcessingRun)
                .where(
                    ProcessingRun.id == claimed_run.id,
                    ProcessingRun.lease_token == lease_token,
                )
                .with_for_update()
            )
            if run is None or run.status in {RUN_READY, RUN_CANCELLED}:
                return
            error_message = f"{type(error).__name__}: {error}"[:2000]
            was_embedding = run.status == RUN_EMBEDDING
            run.last_error = error_message
            run.lease_until = None
            run.lease_token = None
            if permanent or run.attempts >= self._settings.processing_max_attempts:
                run.status = RUN_FAILED
                run.completed_at = utcnow()
                if was_embedding:
                    await session.execute(
                        update(ChunkEmbedding)
                        .where(
                            ChunkEmbedding.processing_run_id == run.id,
                            ChunkEmbedding.embedding_profile == self._settings.embedding_profile,
                            ChunkEmbedding.status != EMBEDDING_INDEXED,
                        )
                        .values(
                            status=EMBEDDING_FAILED,
                            last_error=error_message,
                            updated_at=utcnow(),
                        )
                    )
                logger.error(
                    "Processing run failed permanently: run_id=%s document_id=%s "
                    "workspace_id=%s job=%s attempts=%d error=%s",
                    run.id,
                    run.document_id,
                    run.origin_workspace_id,
                    run.job_kind,
                    run.attempts,
                    error_message,
                    exc_info=True,
                )
                return
            exponent = min(run.attempts - 1, 20)
            delay = min(
                self._settings.processing_retry_base_seconds * (2**exponent),
                self._settings.processing_retry_max_seconds,
            )
            run.status = RUN_RETRY_WAIT
            run.available_at = utcnow() + timedelta(seconds=delay)
            logger.warning(
                "Processing run scheduled for retry: run_id=%s document_id=%s "
                "workspace_id=%s job=%s attempts=%d retry_in=%.1fs error=%s",
                run.id,
                run.document_id,
                run.origin_workspace_id,
                run.job_kind,
                run.attempts,
                delay,
                error_message,
                exc_info=True,
            )
            if was_embedding:
                await session.execute(
                    update(ChunkEmbedding)
                    .where(
                        ChunkEmbedding.processing_run_id == run.id,
                        ChunkEmbedding.embedding_profile == self._settings.embedding_profile,
                        ChunkEmbedding.status != EMBEDDING_INDEXED,
                    )
                    .values(
                        status=EMBEDDING_RETRY_WAIT,
                        last_error=error_message,
                        updated_at=utcnow(),
                    )
                )

    async def _extract_with_heartbeat(
        self,
        run_id: UUID,
        lease_token: UUID,
        path: Path,
        *,
        media_type: str,
    ) -> ExtractionResult:
        stop_event = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat_lease(run_id, lease_token, stop_event))
        try:
            with _tracer.start_as_current_span(
                "extract", attributes={"retriv.media_type": media_type}
            ) as span:
                try:
                    async with asyncio.timeout(self._settings.processing_timeout_seconds):
                        extraction = await to_thread.run_sync(
                            partial(self._extractor.extract, path, media_type=media_type),
                            abandon_on_cancel=True,
                        )
                except Exception as exc:
                    span.record_exception(exc)
                    span.set_status(Status(StatusCode.ERROR, str(exc)))
                    raise
                span.set_attribute("retriv.page_count", extraction.page_count)
                return extraction
        except TimeoutError as exc:
            raise ExtractionError("Document extraction exceeded the time limit") from exc
        finally:
            stop_event.set()
            await heartbeat

    async def _describe_images(
        self,
        run_id: UUID,
        lease_token: UUID,
        extraction: ExtractionResult,
    ) -> ExtractionResult:
        """Describe the document's pictures and put the words back in the chunks.

        A picture that the parser found is a picture the search cannot see:
        a chart's values are in the pixels, not in any text layer, so a
        question about them has nothing to match against. Each picture is
        described by the multimodal model and the description is written into
        the chunk the picture was on, which is then embedded with everything
        else.

        The stage fails the run rather than skipping the pictures. A
        document indexed without its figures is a document that looks
        complete and is not, and nothing downstream can tell the difference.
        """
        provider = self._vision_provider
        images: list[ExtractedImage] = []
        if self._settings.image_processing_enabled:
            images = extraction.images
            if self._settings.vlm_max_images_per_document:
                images = images[: self._settings.vlm_max_images_per_document]
        descriptions: dict[str, str] = {}
        if images:
            if provider is None:
                raise PermanentProcessingError(
                    "Image processing is enabled but no vision provider is configured"
                )
            await self._set_phase(run_id, lease_token, RUN_DESCRIBING)
            stop_event = asyncio.Event()
            heartbeat = asyncio.create_task(self._heartbeat_lease(run_id, lease_token, stop_event))
            try:
                descriptions = await self._describe_all(provider, extraction, images)
            finally:
                stop_event.set()
                await heartbeat
        usable = {
            ref: description
            for ref, description in descriptions.items()
            if not is_no_figure_reply(description)
        }
        chunks = merge_descriptions(
            extraction.chunks,
            {
                ref: ("" if is_no_figure_reply(description) else description)
                for ref, description in descriptions.items()
            },
            images=images,
            tokenizer_name=self._settings.chunk_tokenizer,
            model=self._settings.vlm_model,
        )
        return replace(extraction, chunks=chunks, image_descriptions=usable)

    async def _describe_all(
        self,
        provider: VisionProvider,
        extraction: ExtractionResult,
        images: list[ExtractedImage],
    ) -> dict[str, str]:
        """Describe every picture, a few at a time.

        The concurrency cap belongs to the worker, not this stage: two runs
        describing at once share one budget of in-flight requests.
        """

        async def describe_one(image: ExtractedImage) -> str:
            async with self._vlm_slots:
                return await provider.describe(
                    image, context=self._image_context(extraction.chunks, image)
                )

        with _tracer.start_as_current_span(
            "describe", attributes={"retriv.image_count": len(images)}
        ) as span:
            try:
                async with asyncio.timeout(self._settings.processing_timeout_seconds):
                    results = await asyncio.gather(
                        *(describe_one(image) for image in images),
                        return_exceptions=True,
                    )
            except TimeoutError as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise ExtractionError(
                    "Describing the document's pictures exceeded the time limit"
                ) from exc
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return {
            image.ref: str(description) for image, description in zip(images, results, strict=True)
        }

    @staticmethod
    def _image_context(chunks: list[ExtractedChunk], image: ExtractedImage) -> str:
        """What the model is told about where the picture sits.

        The heading the picture is under and the caption printed next to it
        say what the picture is of, and a description that knows that is
        worth several descriptions that do not.
        """
        for chunk in chunks:
            if image.ref in picture_refs(chunk.text):
                return build_context(chunk.section_path, image.caption)
        return build_context([], image.caption)

    async def _discard_previous_attempt_artifacts(
        self, run: ProcessingRun, lease_token: UUID
    ) -> None:
        if run.artifact_key is None and run.manifest_key is None:
            return
        prefix = next(
            (key.rsplit("/", 1)[0] for key in (run.artifact_key, run.manifest_key) if key),
            None,
        )
        if prefix is not None:
            await to_thread.run_sync(
                partial(
                    self._storage.delete_prefix,
                    bucket=self._settings.s3_artifact_bucket,
                    prefix=f"{prefix}/",
                )
            )
        async with self._session_factory() as session:
            result = await session.execute(
                update(ProcessingRun)
                .where(
                    ProcessingRun.id == run.id,
                    ProcessingRun.lease_token == lease_token,
                )
                .values(artifact_bucket=None, artifact_key=None, manifest_key=None)
            )
            await session.commit()
            if getattr(result, "rowcount", 0) != 1:
                raise LeaseLostError()

    async def _heartbeat_lease(
        self, run_id: UUID, lease_token: UUID | None, stop_event: asyncio.Event
    ) -> None:
        """Extend the run's lease until the caller's work is done.

        A missing token matches no row, so the heartbeat exits on its first
        tick -- the same outcome as losing the lease.
        """
        interval = max(1.0, self._settings.processing_lease_seconds / 3)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            async with self._session_factory() as session:
                result = await session.execute(
                    update(ProcessingRun)
                    .where(
                        ProcessingRun.id == run_id,
                        ProcessingRun.lease_token == lease_token,
                        ProcessingRun.status.in_(ACTIVE_RUN_STATUSES),
                    )
                    .values(
                        lease_until=utcnow()
                        + timedelta(seconds=self._settings.processing_lease_seconds)
                    )
                )
                await session.commit()
                if getattr(result, "rowcount", 0) != 1:
                    return

    @staticmethod
    def _require_lease_token(run: ProcessingRun) -> UUID:
        if run.lease_token is None:
            raise LeaseLostError()
        return run.lease_token

    async def _delete_artifacts(self, artifact_key: str | None, manifest_key: str | None) -> None:
        prefix = next(
            (key.rsplit("/", 1)[0] for key in (artifact_key, manifest_key) if key is not None),
            None,
        )
        if prefix is None:
            return
        with suppress(StorageError):
            await to_thread.run_sync(
                partial(
                    self._storage.delete_prefix,
                    bucket=self._settings.s3_artifact_bucket,
                    prefix=f"{prefix}/",
                )
            )
