import asyncio
import gzip
import hashlib
import json
import logging
import os
import tempfile
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from anyio import to_thread
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import (
    ACTIVE_RUN_STATUSES,
    EMBEDDING_FAILED,
    EMBEDDING_INDEXED,
    EMBEDDING_PENDING,
    EMBEDDING_PROCESSING,
    EMBEDDING_RETRY_WAIT,
    JOB_DELETE,
    JOB_EXTRACT,
    RUN_CANCELLED,
    RUN_CHUNKING,
    RUN_DELETING,
    RUN_EMBEDDING,
    RUN_EXTRACTING,
    RUN_FAILED,
    RUN_FETCHING,
    RUN_QUEUED,
    RUN_READY,
    RUN_RETRY_WAIT,
    RUN_STORING_ARTIFACT,
    ChunkEmbedding,
    Document,
    DocumentChunk,
    ProcessingRun,
)
from app.embeddings.base import (
    EmbeddingProvider,
    EmbeddingProviderError,
    VectorPoint,
    VectorStore,
    VectorStoreError,
)
from app.extraction.base import DocumentExtractor, ExtractionError, ExtractionResult
from app.infrastructure.storage import (
    ArtifactStorage,
    ObjectNotFoundError,
    ObjectTooLargeError,
    StorageError,
)

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._session_factory = session_factory
        self._storage = storage
        self._extractor = extractor
        self._settings = settings
        self._embedding_provider = embedding_provider
        self._vector_store = vector_store
        self._next_artifact_reconciliation = datetime.min.replace(tzinfo=UTC)
        self._next_embedding_reconciliation = datetime.min.replace(tzinfo=UTC)

    async def run_once(self) -> bool:
        await self._recover_expired_leases()
        await self._reconcile_deleted_artifacts()
        await self._queue_embedding_backfill()
        run = await self._claim_one()
        if run is None:
            return False
        try:
            if run.job_kind == JOB_DELETE:
                await self._process_delete(run)
            else:
                await self._process_extract(run)
        except LeaseLostError:
            logger.warning("Processing lease was lost", extra={"run_id": str(run.id)})
        except (PermanentProcessingError, ObjectNotFoundError, ObjectTooLargeError) as exc:
            await self._record_failure(run, exc, permanent=True)
        except (EmbeddingProviderError, VectorStoreError) as exc:
            await self._record_failure(run, exc, permanent=not exc.retryable)
        except (StorageError, ExtractionError, OSError, ValueError) as exc:
            await self._record_failure(run, exc, permanent=False)
        except Exception as exc:
            logger.exception("Unexpected processing failure", extra={"run_id": str(run.id)})
            await self._record_failure(run, exc, permanent=False)
        return True

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            processed = await self.run_once()
            if processed:
                continue
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._settings.processing_poll_interval_seconds,
                )
            except TimeoutError:
                continue

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
        now = utcnow()
        if now < self._next_artifact_reconciliation:
            return
        self._next_artifact_reconciliation = now + timedelta(
            seconds=self._settings.artifact_reconciliation_interval_seconds
        )
        async with self._session_factory() as session:
            result = await session.execute(
                select(Document.workspace_id, Document.id).where(
                    Document.deleted_at.is_not(None)
                )
            )
            tombstones = result.all()
        for workspace_id, document_id in tombstones:
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
                await self._vector_store.delete_document(workspace_id, document_id)
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
        async with self._session_factory() as session, session.begin():
            result = await session.scalars(
                select(ProcessingRun)
                .join(Document, Document.id == ProcessingRun.document_id)
                .where(
                    ProcessingRun.job_kind == JOB_EXTRACT,
                    ProcessingRun.status == RUN_READY,
                    Document.deleted_at.is_(None),
                    Document.active_processing_run_id == ProcessingRun.id,
                )
                .order_by(ProcessingRun.created_at, ProcessingRun.id)
                .limit(100)
                .with_for_update(of=ProcessingRun, skip_locked=True)
            )
            for run in result.all():
                chunks = (
                    await session.scalars(
                        select(DocumentChunk)
                        .where(DocumentChunk.processing_run_id == run.id)
                        .order_by(DocumentChunk.ordinal)
                    )
                ).all()
                if not chunks:
                    continue
                embeddings = (
                    await session.scalars(
                        select(ChunkEmbedding).where(
                            ChunkEmbedding.processing_run_id == run.id,
                            ChunkEmbedding.embedding_profile
                            == self._settings.embedding_profile,
                        )
                    )
                ).all()
                by_chunk_id = {embedding.chunk_id: embedding for embedding in embeddings}
                indexed_count = 0
                for chunk in chunks:
                    embedding = by_chunk_id.get(chunk.id)
                    if embedding is None:
                        session.add(self._embedding_record(chunk))
                        continue
                    if (
                        embedding.status == EMBEDDING_INDEXED
                        and embedding.content_hash == chunk.content_hash
                        and embedding.model == self._settings.embedding_model
                        and embedding.dimensions == self._settings.embedding_dimensions
                    ):
                        indexed_count += 1
                        continue
                    embedding.model = self._settings.embedding_model
                    embedding.dimensions = self._settings.embedding_dimensions
                    embedding.content_hash = chunk.content_hash
                    embedding.qdrant_point_id = self._qdrant_point_id(chunk.id)
                    embedding.status = EMBEDDING_PENDING
                    embedding.last_error = None
                    embedding.embedded_at = None
                    embedding.updated_at = now
                if indexed_count == len(chunks):
                    continue
                run.status = RUN_QUEUED
                run.available_at = now
                run.completed_at = None
                run.last_error = None
                run.chunk_count = len(chunks)
                run.embedded_chunk_count = indexed_count
                run.embedding_profile = self._settings.embedding_profile
                run.embedding_model = self._settings.embedding_model
                run.embedding_dimensions = self._settings.embedding_dimensions

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
            f"workspaces/{document.workspace_id}/objects/"
            f"{document.sha256}/{document.id}"
        )
        if document.object_key != expected_key:
            raise PermanentProcessingError("Object key does not match document identity")
        if document.size_bytes > self._settings.max_source_bytes:
            raise PermanentProcessingError("Document exceeds the processing size limit")

        self._settings.processing_temp_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".pdf" if document.detected_media_type == "application/pdf" else ".docx"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="context-source-",
            suffix=suffix,
            dir=self._settings.processing_temp_dir,
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        artifact_key: str | None = None
        manifest_key: str | None = None
        chunks_persisted = False
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
            extraction = await self._extract_with_heartbeat(
                claimed_run.id,
                lease_token,
                temporary_path,
                media_type=document.detected_media_type,
            )
            self._validate_extraction(extraction)

            artifact_prefix = (
                f"workspaces/{document.workspace_id}/documents/{document.id}/"
                f"runs/{claimed_run.id}/attempts/{lease_token}"
            )
            artifact_key = f"{artifact_prefix}/docling-document.json.gz"
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
            await self._set_phase(
                claimed_run.id, lease_token, RUN_STORING_ARTIFACT
            )
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
            await self._set_phase(
                claimed_run.id, lease_token, RUN_STORING_ARTIFACT
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

            await self._set_phase(claimed_run.id, lease_token, RUN_CHUNKING)
            chunks_persisted = await self._persist_chunks(
                claimed_run.id,
                lease_token,
                document,
                extraction,
                artifact_key=artifact_key,
                manifest_key=manifest_key,
            )
            if not chunks_persisted:
                await self._delete_artifacts(artifact_key, manifest_key)
        except LeaseLostError:
            if artifact_key is not None and manifest_key is not None:
                await self._delete_artifacts(artifact_key, manifest_key)
            raise
        except Exception:
            if artifact_key is not None and manifest_key is not None:
                await self._delete_artifacts(artifact_key, manifest_key)
            raise
        finally:
            await to_thread.run_sync(partial(temporary_path.unlink, missing_ok=True))
        if chunks_persisted:
            await self._process_embeddings(claimed_run, lease_token)

    async def _process_delete(self, claimed_run: ProcessingRun) -> None:
        lease_token = self._require_lease_token(claimed_run)
        await self._vector_store.delete_document(
            claimed_run.workspace_id, claimed_run.document_id
        )
        artifact_prefix = (
            f"workspaces/{claimed_run.workspace_id}/documents/"
            f"{claimed_run.document_id}/"
        )
        await to_thread.run_sync(
            partial(
                self._storage.delete_prefix,
                bucket=self._settings.s3_artifact_bucket,
                prefix=artifact_prefix,
            )
        )
        await self._set_phase(claimed_run.id, lease_token, RUN_DELETING)

        # The Qdrant points are gone and the artifacts are gone, so the
        # rows that recorded where they lived are no longer needed. The
        # document row goes last, and its cascade takes the chunks and the
        # embedding ledger with it. Deleting it earlier would have
        # orphaned the vectors, because the point IDs live in those rows.
        #
        # The run is marked terminal before the document is removed and
        # nothing is written to it afterwards: the cascade takes this run
        # too, so an UPDATE after the DELETE would match no row.
        async with self._session_factory() as session, session.begin():
            run = await session.get(ProcessingRun, claimed_run.id)
            if run is None or run.lease_token != lease_token:
                raise LeaseLostError()
            run.status = RUN_READY
            run.lease_until = None
            run.lease_token = None
            run.completed_at = utcnow()
            document = await session.scalar(
                select(Document)
                .where(Document.id == claimed_run.document_id)
                .with_for_update()
            )
            if document is not None:
                await session.delete(document)

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
        extracted_text_bytes = sum(
            len(chunk.text.encode("utf-8")) for chunk in extraction.chunks
        )
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
            # No text layer to compare against: an image-only PDF, or a format whose
            # text only exists after OCR. There is no signal here, and inventing one
            # would reject legitimately short documents.
            return
        extracted_chars = sum(len(chunk.text) for chunk in extraction.chunks)
        ratio = extracted_chars / source_chars
        if ratio < minimum:
            raise PermanentProcessingError(
                f"Extraction kept only {ratio:.0%} of the text in this document "
                f"({extracted_chars} of {source_chars} characters); "
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
            "artifact_type": "docling-document",
            "artifact_schema_version": 1,
            "workspace_id": str(document.workspace_id),
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
                "docling_version": extraction.extractor_version,
                # The stage decision the extractor actually made. Previously this was
                # derived from the media type, which claimed OCR ran for every PDF.
                "ocr_enabled": extraction.ocr_enabled,
                "ocr_languages": self._settings.ocr_languages,
                "chunk_max_tokens": self._settings.chunk_max_tokens,
                "source_text_chars": extraction.source_text_chars,
                "extracted_text_chars": sum(
                    len(chunk.text) for chunk in extraction.chunks
                ),
            },
            "artifact": {
                "bucket": self._settings.s3_artifact_bucket,
                "key": artifact_key,
                "content_type": "application/json",
                "content_encoding": "gzip",
                "sha256": artifact_sha256,
            },
            "chunk_count": len(extraction.chunks),
        }

    async def _process_embeddings(
        self, claimed_run: ProcessingRun, lease_token: UUID
    ) -> None:
        stop_event = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat_lease(claimed_run.id, lease_token, stop_event)
        )
        try:
            await self._vector_store.ensure_collection()
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
                            "workspace_id": str(chunk.workspace_id),
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
                        await self._vector_store.delete_points(
                            [point.id for point in points]
                        )
                    raise
            activated = await self._activate_embedded_run(
                claimed_run.id, lease_token
            )
            if not activated:
                await self._vector_store.delete_document(
                    claimed_run.workspace_id, claimed_run.document_id
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
                        ChunkEmbedding.embedding_profile
                        == self._settings.embedding_profile,
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
            if (
                run is None
                or run.lease_token != lease_token
                or run.status != RUN_EMBEDDING
            ):
                raise LeaseLostError()
            result = await session.execute(
                update(ChunkEmbedding)
                .where(
                    ChunkEmbedding.processing_run_id == run_id,
                    ChunkEmbedding.chunk_id.in_(chunk_ids),
                    ChunkEmbedding.embedding_profile
                    == self._settings.embedding_profile,
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
            if (
                run is None
                or run.lease_token != lease_token
                or run.status != RUN_EMBEDDING
            ):
                raise LeaseLostError()
            document = await session.get(Document, run.document_id)
            if (
                document is None
                or document.deleted_at is not None
            ):
                raise LeaseLostError()
            result = await session.execute(
                update(ChunkEmbedding)
                .where(
                    ChunkEmbedding.processing_run_id == run_id,
                    ChunkEmbedding.chunk_id.in_(chunk_ids),
                    ChunkEmbedding.embedding_profile
                    == self._settings.embedding_profile,
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
            indexed_count = await session.scalar(
                select(func.count())
                .select_from(ChunkEmbedding)
                .where(
                    ChunkEmbedding.processing_run_id == run_id,
                    ChunkEmbedding.embedding_profile
                    == self._settings.embedding_profile,
                    ChunkEmbedding.status == EMBEDDING_INDEXED,
                    ChunkEmbedding.model == self._settings.embedding_model,
                    ChunkEmbedding.dimensions == self._settings.embedding_dimensions,
                )
            )
            run.embedded_chunk_count = int(indexed_count or 0)

    async def _activate_embedded_run(
        self, run_id: UUID, lease_token: UUID
    ) -> bool:
        now = utcnow()
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
                    ChunkEmbedding.embedding_profile
                    == self._settings.embedding_profile,
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
            run.chunk_count = int(chunk_count)
            run.embedded_chunk_count = int(indexed_count or 0)
            run.completed_at = now
            document.active_processing_run_id = run.id
            return True

    async def _persist_chunks(
        self,
        run_id: UUID,
        lease_token: UUID,
        document: Document,
        extraction: ExtractionResult,
        *,
        artifact_key: str,
        manifest_key: str,
    ) -> bool:
        now = utcnow()
        async with self._session_factory() as session, session.begin():
            stored_document = await session.scalar(
                select(Document)
                .where(Document.id == document.id)
                .with_for_update()
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
                delete(ChunkEmbedding).where(
                    ChunkEmbedding.processing_run_id == run_id
                )
            )
            await session.execute(
                delete(DocumentChunk).where(DocumentChunk.processing_run_id == run_id)
            )
            stored_chunks: list[DocumentChunk] = []
            for ordinal, chunk in enumerate(extraction.chunks):
                content_hash = hashlib.sha256(chunk.text.encode()).hexdigest()
                stored_chunk = DocumentChunk(
                    id=uuid5(run_id, f"{ordinal}:{content_hash}"),
                    workspace_id=document.workspace_id,
                    document_id=document.id,
                    processing_run_id=run_id,
                    ordinal=ordinal,
                    text=chunk.text,
                    token_count=chunk.token_count,
                    section_path=chunk.section_path,
                    page_numbers=chunk.page_numbers,
                    provenance=chunk.provenance,
                    content_hash=content_hash,
                )
                session.add(stored_chunk)
                stored_chunks.append(stored_chunk)
            # No ORM relationship links these rows: flush parents before FK children.
            # Both writes remain in the same transaction and roll back together.
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
        return ChunkEmbedding(
            chunk_id=chunk.id,
            embedding_profile=self._settings.embedding_profile,
            workspace_id=chunk.workspace_id,
            document_id=chunk.document_id,
            processing_run_id=chunk.processing_run_id,
            model=self._settings.embedding_model,
            dimensions=self._settings.embedding_dimensions,
            content_hash=chunk.content_hash,
            qdrant_point_id=self._qdrant_point_id(chunk.id),
            status=EMBEDDING_PENDING,
        )

    def _qdrant_point_id(self, chunk_id: UUID) -> UUID:
        return uuid5(chunk_id, self._settings.embedding_profile)

    async def _set_phase(
        self, run_id: UUID, lease_token: UUID, status: str
    ) -> None:
        lease_until = utcnow() + timedelta(
            seconds=self._settings.processing_lease_seconds
        )
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
        artifact_key: str,
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
                            ChunkEmbedding.embedding_profile
                            == self._settings.embedding_profile,
                            ChunkEmbedding.status != EMBEDDING_INDEXED,
                        )
                        .values(
                            status=EMBEDDING_FAILED,
                            last_error=error_message,
                            updated_at=utcnow(),
                        )
                    )
                return
            exponent = min(run.attempts - 1, 20)
            delay = min(
                self._settings.processing_retry_base_seconds * (2**exponent),
                self._settings.processing_retry_max_seconds,
            )
            run.status = RUN_RETRY_WAIT
            run.available_at = utcnow() + timedelta(seconds=delay)
            if was_embedding:
                await session.execute(
                    update(ChunkEmbedding)
                    .where(
                        ChunkEmbedding.processing_run_id == run.id,
                        ChunkEmbedding.embedding_profile
                        == self._settings.embedding_profile,
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
        heartbeat = asyncio.create_task(
            self._heartbeat_lease(run_id, lease_token, stop_event)
        )
        try:
            try:
                async with asyncio.timeout(
                    self._settings.processing_timeout_seconds
                ):
                    return await to_thread.run_sync(
                        partial(self._extractor.extract, path, media_type=media_type),
                        abandon_on_cancel=True,
                    )
            except TimeoutError as exc:
                raise ExtractionError("Document extraction exceeded the time limit") from exc
        finally:
            stop_event.set()
            await heartbeat

    async def _discard_previous_attempt_artifacts(
        self, run: ProcessingRun, lease_token: UUID
    ) -> None:
        if run.artifact_key is None and run.manifest_key is None:
            return
        for key in (run.artifact_key, run.manifest_key):
            if key is not None:
                await to_thread.run_sync(
                    partial(
                        self._storage.delete_object,
                        bucket=self._settings.s3_artifact_bucket,
                        key=key,
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
        self, run_id: UUID, lease_token: UUID, stop_event: asyncio.Event
    ) -> None:
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

    async def _delete_artifacts(self, artifact_key: str, manifest_key: str) -> None:
        for key in (artifact_key, manifest_key):
            with suppress(StorageError):
                await to_thread.run_sync(
                    partial(
                        self._storage.delete_object,
                        bucket=self._settings.s3_artifact_bucket,
                        key=key,
                    )
                )
