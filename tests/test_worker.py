"""The processing worker.

Driven directly against the models rather than through the intake
webhook, which no longer exists. The important case here is the delete
path: the document row must outlive the request so the worker can read
the Qdrant point IDs out of the embedding ledger, and must be removed
only after the vectors are gone.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import (
    EMBEDDING_INDEXED,
    EMBEDDING_RETRY_WAIT,
    JOB_DELETE,
    JOB_EXTRACT,
    RUN_CANCELLED,
    RUN_EXTRACTING,
    RUN_FAILED,
    RUN_FETCHING,
    RUN_QUEUED,
    RUN_READY,
    RUN_RETRY_WAIT,
    ChunkEmbedding,
    Document,
    DocumentChunk,
    DocumentRow,
    ProcessingRun,
)
from app.embeddings.base import (
    EmbeddingProvider,
    EmbeddingProviderError,
    VectorMatch,
    VectorPoint,
    VectorStore,
)
from app.extraction.base import ExtractedChunk, ExtractedImage, ExtractionResult
from app.extraction.images import picture_marker
from app.infrastructure.storage import (
    ArtifactStorage,
    DownloadResult,
    ObjectNotFoundError,
    ObjectTooLargeError,
)
from app.processing.worker import ProcessingWorker, utcnow
from app.sections import encode_section_key
from app.vision.base import VisionProviderError
from tests.conftest import SeededEntities
from tests.document_samples import (
    csv_bytes,
    csv_semicolon_bytes,
    csv_with_bom_bytes,
)

pytestmark = pytest.mark.asyncio


class FakeStorage(ArtifactStorage):
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.prefixes: list[tuple[str, str]] = []
        self.download_error: Exception | None = None
        self.put_error: Exception | None = None

    def download_file(
        self,
        destination: Path,
        *,
        bucket: str,
        key: str,
        max_bytes: int,
    ) -> DownloadResult:

        if self.download_error is not None:
            raise self.download_error
        try:
            data = self.objects[(bucket, key)]
        except KeyError as exc:
            raise ObjectNotFoundError(f"no such object {bucket}/{key}") from exc
        if len(data) > max_bytes:
            raise ObjectTooLargeError("too large")
        destination = Path(destination)
        destination.write_bytes(data)
        return DownloadResult(size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())

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
        if self.put_error is not None:
            raise self.put_error
        self.objects[(bucket, key)] = data

    def delete_object(self, *, bucket: str, key: str) -> None:
        self.objects.pop((bucket, key), None)

    def delete_prefix(self, *, bucket: str, prefix: str) -> None:
        self.prefixes.append((bucket, prefix))
        for key in [k for (b, k) in self.objects if b == bucket and k.startswith(prefix)]:
            del self.objects[(bucket, key)]


class FakeExtractor:
    def __init__(
        self,
        chunk_texts: list[str] | None = None,
        *,
        ocr_enabled: bool = False,
        source_text_chars: int = 0,
        images: list[ExtractedImage] | None = None,
    ) -> None:
        self.chunk_texts = chunk_texts or ["Extracted paragraph one."]
        self.calls = 0
        self.media_types: list[str] = []
        self.ocr_enabled = ocr_enabled
        self.source_text_chars = source_text_chars
        self.images = images or []

    def extract(self, path: Path, *, media_type: str) -> ExtractionResult:
        self.calls += 1
        self.media_types.append(media_type)
        return ExtractionResult(
            document_json=b'{"origin": "test"}',
            chunks=[
                ExtractedChunk(
                    text=text,
                    token_count=len(text.split()),
                    section_path=["Body"],
                    section_key=encode_section_key(["Body"]),
                    is_heading_only=False,
                    page_numbers=[1],
                    provenance={},
                )
                for text in self.chunk_texts
            ],
            extractor_version="test-extractor-1",
            ocr_enabled=self.ocr_enabled,
            source_text_chars=self.source_text_chars,
            images=list(self.images),
        )


class FakeVisionProvider:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.described: list[str] = []
        self.contexts: list[str] = []

    async def describe(self, image: ExtractedImage, *, context: str = "") -> str:
        if self._error is not None:
            raise self._error
        self.described.append(image.ref)
        self.contexts.append(context)
        return f"A description of {image.ref}."

    async def aclose(self) -> None:
        return


class FakeEmbeddingProvider(EmbeddingProvider):
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.embedded: list[str] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self._error is not None:
            raise self._error
        self.embedded.extend(texts)
        return [[0.1, 0.2] for _ in texts]

    async def aclose(self) -> None:
        return


class FakeVectorStore(VectorStore):
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.points: dict[str, VectorPoint] = {}
        self.deleted_documents: list[tuple[UUID, UUID]] = []
        self.deleted_points: list[UUID] = []

    async def ensure_collection(self) -> None:
        return

    async def upsert(self, points: list[VectorPoint]) -> None:
        if self._error is not None:
            raise self._error
        for point in points:
            self.points[str(point.id)] = point

    async def delete_points(self, point_ids: list[UUID]) -> None:
        self.deleted_points.extend(point_ids)
        for point_id in point_ids:
            self.points.pop(str(point_id), None)

    async def delete_document(self, workspace_id: UUID, document_id: UUID) -> None:
        self.deleted_documents.append((workspace_id, document_id))
        for key, point in list(self.points.items()):
            if (
                point.payload.get("workspace_id") == str(workspace_id)
                and point.payload.get("document_id") == str(document_id)
            ):
                del self.points[key]

    async def search(
        self,
        vector: list[float],
        *,
        workspace_ids: list[UUID],
        embedding_profile: str,
        limit: int,
    ) -> list[VectorMatch]:  # pragma: no cover - the worker never searches
        raise NotImplementedError

    async def aclose(self) -> None:
        return


def build_worker(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    storage: FakeStorage | None = None,
    extractor: FakeExtractor | None = None,
    embeddings: FakeEmbeddingProvider | None = None,
    vectors: FakeVectorStore | None = None,
    vision: FakeVisionProvider | None = None,
) -> ProcessingWorker:
    return ProcessingWorker(
        session_factory,
        storage or FakeStorage(),
        extractor or FakeExtractor(),
        settings,
        embeddings or FakeEmbeddingProvider(),
        vectors or FakeVectorStore(),
        vision,
    )


async def seed_document(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: UUID,
    *,
    storage: FakeStorage | None = None,
    content: bytes = b"a representative PDF",
    media_type: str = "application/pdf",
    filename: str = "report.pdf",
) -> tuple[Document, ProcessingRun]:
    """Insert the document and its extract run the way the upload route does."""
    document_id = uuid4()
    digest = hashlib.sha256(content).hexdigest()
    object_key = f"workspaces/{workspace_id}/objects/{digest}/{document_id}"
    document = Document(
        id=document_id,
        workspace_id=workspace_id,
        original_filename=filename,
        detected_media_type=media_type,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        bucket="raw-documents",
        object_key=object_key,
    )
    run = ProcessingRun(
        id=uuid4(),
        document_id=document_id,
        workspace_id=workspace_id,
        job_kind=JOB_EXTRACT,
        pipeline_version="2026.1",
        status=RUN_QUEUED,
    )
    document.active_processing_run_id = run.id
    if storage is not None:
        storage.objects[("raw-documents", object_key)] = content
    async with session_factory() as session, session.begin():
        session.add_all([document, run])
    return document, run


async def test_worker_indexes_a_document_end_to_end(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    storage = FakeStorage()
    document, run = await seed_document(
        db_session_factory, entities.finance.id, storage=storage
    )
    vectors = FakeVectorStore()
    worker = build_worker(db_session_factory, settings, storage=storage, vectors=vectors)

    assert await worker.run_once() is True

    async with db_session_factory() as session:
        finished = await session.get(ProcessingRun, run.id)
        chunks = (
            await session.scalars(
                select(DocumentChunk).where(
                    DocumentChunk.document_id == document.id
                )
            )
        ).all()
        embeddings = (
            await session.scalars(
                select(ChunkEmbedding).where(
                    ChunkEmbedding.document_id == document.id
                )
            )
        ).all()

    assert finished is not None
    assert finished.status == RUN_READY
    assert finished.chunk_count == 1
    assert finished.embedded_chunk_count == 1
    assert [chunk.text for chunk in chunks] == ["Extracted paragraph one."]
    assert all(embedding.status == EMBEDDING_INDEXED for embedding in embeddings)
    # the artifact landed under the workspace/document prefix
    assert any(
        key.startswith(f"workspaces/{entities.finance.id}/documents/{document.id}/")
        for _bucket, key in storage.objects
    )
    # and the vector is in the store
    assert len(vectors.points) == 1


async def test_a_completed_run_records_its_progress_and_its_attempt(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """A finished run says how far it got and where its artifacts landed.

    The progress counters are what the dashboard reads, and the artifact
    key is attempt-scoped so a superseded attempt can clean up after
    itself without touching the current one.
    """
    storage = FakeStorage()
    document, run = await seed_document(
        db_session_factory, entities.finance.id, storage=storage
    )
    worker = build_worker(
        db_session_factory, settings, storage=storage, extractor=FakeExtractor()
    )

    assert await worker.run_once() is True

    async with db_session_factory() as session:
        finished = await session.get(ProcessingRun, run.id)
    assert finished is not None
    assert finished.status == RUN_READY
    assert finished.started_at is not None
    assert finished.completed_at is not None
    assert finished.chunk_count == 1
    assert finished.embedded_chunk_count == 1
    assert finished.embedding_profile == settings.embedding_profile
    assert finished.embedding_model == settings.embedding_model
    assert finished.embedding_dimensions == settings.embedding_dimensions
    assert finished.artifact_bucket == settings.s3_artifact_bucket
    assert finished.artifact_key is not None
    assert finished.artifact_key.startswith(
        f"workspaces/{entities.finance.id}/documents/{document.id}/runs/{run.id}/"
    )
    assert finished.manifest_key is not None
    assert finished.last_error is None


async def test_an_expired_lease_is_reclaimed_and_retried(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    storage = FakeStorage()
    _, run = await seed_document(
        db_session_factory, entities.finance.id, storage=storage
    )
    async with db_session_factory() as session, session.begin():
        stale = await session.get(ProcessingRun, run.id)
        assert stale is not None
        stale.status = RUN_FETCHING
        stale.lease_until = datetime.now(UTC) - timedelta(minutes=5)
        stale.lease_token = uuid4()
        stale.attempts = 1

    worker = build_worker(db_session_factory, settings, storage=storage)
    assert await worker.run_once() is True

    async with db_session_factory() as session:
        finished = await session.get(ProcessingRun, run.id)
    assert finished is not None
    assert finished.status == RUN_READY
    assert finished.lease_token is None
    assert finished.lease_until is None


async def test_a_run_exhausting_its_attempts_is_failed(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    storage = FakeStorage()
    _, run = await seed_document(
        db_session_factory, entities.finance.id, storage=storage
    )
    async with db_session_factory() as session, session.begin():
        stale = await session.get(ProcessingRun, run.id)
        assert stale is not None
        stale.status = RUN_FETCHING
        stale.lease_until = datetime.now(UTC) - timedelta(minutes=5)
        stale.attempts = settings.processing_max_attempts

    worker = build_worker(db_session_factory, settings, storage=storage)
    assert await worker.run_once() is False, "the run is dead-lettered, not processed"

    async with db_session_factory() as session:
        failed = await session.get(ProcessingRun, run.id)
    assert failed is not None
    assert failed.status == RUN_FAILED
    assert "expired" in (failed.last_error or "").lower() or "attempt" in (
        failed.last_error or ""
    ).lower()


async def test_a_retryable_storage_error_schedules_a_backoff(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    storage = FakeStorage()
    await seed_document(db_session_factory, entities.finance.id, storage=storage)
    storage.download_error = OSError("transient network failure")

    worker = build_worker(db_session_factory, settings, storage=storage)
    assert await worker.run_once() is True

    async with db_session_factory() as session:
        run = await session.scalar(select(ProcessingRun))
    assert run is not None
    assert run.status == RUN_RETRY_WAIT
    assert run.attempts == 1
    now = run.available_at
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    assert now > datetime.now(UTC), "a backoff must push the run into the future"
    assert "transient" in (run.last_error or "")


async def test_a_permanently_missing_object_fails_without_retry(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    storage = FakeStorage()
    await seed_document(
        db_session_factory, entities.finance.id, storage=storage
    )
    storage.objects.clear()

    worker = build_worker(db_session_factory, settings, storage=storage)
    assert await worker.run_once() is True

    async with db_session_factory() as session:
        run = await session.scalar(select(ProcessingRun))
    assert run is not None
    assert run.status == RUN_FAILED


async def test_a_lossy_extraction_fails_instead_of_indexing_a_partial_document(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """A parse that silently drops most of the text must not be indexed.

    Nothing downstream can distinguish a partially parsed document from a genuinely
    short one, so without an explicit check a parser regression turns into a quietly
    incomplete index instead of a failed run.
    """
    storage = FakeStorage()
    _, run = await seed_document(
        db_session_factory, entities.finance.id, storage=storage
    )
    extractor = FakeExtractor(
        chunk_texts=["Only a fraction of the document survived."],
        source_text_chars=100_000,
    )
    vectors = FakeVectorStore()

    worker = build_worker(
        db_session_factory, settings, storage=storage, extractor=extractor, vectors=vectors
    )
    assert await worker.run_once() is True, "the run advances, then fails validation"

    async with db_session_factory() as session:
        failed = await session.get(ProcessingRun, run.id)
    assert failed is not None
    assert failed.status == RUN_FAILED
    assert "of the text in this document" in (failed.last_error or "")
    assert vectors.points == {}, "no vectors may be written for a lossy extraction"


async def test_extraction_coverage_is_not_enforced_without_a_text_layer(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """Image-only PDFs have no text layer to compare against, so they are not judged.

    Rejecting them would mean refusing every scan, which is the opposite of what the
    guardrail is for.
    """
    storage = FakeStorage()
    _, run = await seed_document(
        db_session_factory, entities.finance.id, storage=storage
    )
    extractor = FakeExtractor(
        chunk_texts=["Short scanned note."], ocr_enabled=True, source_text_chars=0
    )

    worker = build_worker(
        db_session_factory, settings, storage=storage, extractor=extractor
    )
    assert await worker.run_once() is True

    async with db_session_factory() as session:
        finished = await session.get(ProcessingRun, run.id)
    assert finished is not None
    assert finished.status == RUN_READY


async def test_an_embedding_error_schedules_a_backoff(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    storage = FakeStorage()
    await seed_document(db_session_factory, entities.finance.id, storage=storage)
    worker = build_worker(
        db_session_factory,
        settings,
        storage=storage,
        embeddings=FakeEmbeddingProvider(
            error=EmbeddingProviderError("embedding service down", retryable=True)
        ),
    )

    assert await worker.run_once() is True
    async with db_session_factory() as session:
        run = await session.scalar(select(ProcessingRun))
        embeddings = (await session.scalars(select(ChunkEmbedding))).all()

    assert run is not None
    assert run.status in {RUN_RETRY_WAIT, RUN_EXTRACTING}
    # the chunks are persisted, so a retry resumes at embedding
    assert len(embeddings) == 1
    assert embeddings[0].status == EMBEDDING_RETRY_WAIT


# --- the delete path ----------------------------------------------------


async def test_delete_removes_vectors_before_the_document_row(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """The ordering is the whole point.

    The Qdrant point IDs live in chunk_embeddings, which cascades away
    with the document. If the row were deleted first, the vectors would
    be orphaned with no record of where to find them.
    """
    storage = FakeStorage()
    document, extract_run = await seed_document(
        db_session_factory, entities.finance.id, storage=storage
    )
    vectors = FakeVectorStore()
    indexer = build_worker(
        db_session_factory, settings, storage=storage, vectors=vectors
    )
    assert await indexer.run_once() is True

    # the document is now indexed and has a real vector
    assert len(vectors.points) == 1

    # queue the delete the way the route does
    from app.processing.service import enqueue_delete

    async with db_session_factory() as session, session.begin():
        stored = await session.get(Document, document.id)
        assert stored is not None
        assert stored is not None
        await enqueue_delete(session, stored, settings)

    async with db_session_factory() as session:
        assert await session.get(Document, document.id) is not None, (
            "the row must survive the request so the worker can read the "
            "point IDs"
        )

    deleter = build_worker(
        db_session_factory, settings, storage=storage, vectors=vectors
    )
    assert await deleter.run_once() is True

    assert (entities.finance.id, document.id) in vectors.deleted_documents, (
        "the vector store was never told to drop the document's points"
    )
    assert not vectors.points, "no vector survives the delete"
    assert any(
        prefix == f"workspaces/{entities.finance.id}/documents/{document.id}/"
        for _bucket, prefix in storage.prefixes
    ), "the artifact prefix was removed"

    async with db_session_factory() as session:
        assert await session.get(Document, document.id) is None
        chunks = (
            await session.scalars(
                select(DocumentChunk).where(DocumentChunk.document_id == document.id)
            )
        ).all()
        embeddings = (
            await session.scalars(
                select(ChunkEmbedding).where(
                    ChunkEmbedding.document_id == document.id
                )
            )
        ).all()
    assert chunks == []
    assert embeddings == [], "the ledger went with the document, after the points did"

    del extract_run


async def test_delete_cancels_an_extraction_still_in_flight(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    from app.processing.service import enqueue_delete

    storage = FakeStorage()
    document, extract_run = await seed_document(
        db_session_factory, entities.finance.id, storage=storage
    )
    async with db_session_factory() as session, session.begin():
        stored = await session.get(Document, document.id)
        assert stored is not None
        assert stored is not None
        await enqueue_delete(session, stored, settings)

    async with db_session_factory() as session:
        cancelled = await session.get(ProcessingRun, extract_run.id)
        delete_run = await session.scalar(
            select(ProcessingRun).where(ProcessingRun.job_kind == JOB_DELETE)
        )
    assert cancelled is not None
    assert cancelled.status == RUN_CANCELLED
    assert delete_run is not None
    assert delete_run.status == RUN_QUEUED
    # re-read: the tombstone was written in another session
    async with db_session_factory() as session:
        reloaded = await session.get(Document, document.id)
    assert reloaded is not None
    assert reloaded.deleted_at is not None


async def test_a_delete_run_is_never_cancelled_by_its_own_enqueue(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """Deleting twice must not cancel the queued cleanup, or the vectors
    would be left behind with nothing coming to remove them."""
    from app.processing.service import enqueue_delete

    storage = FakeStorage()
    document, _ = await seed_document(
        db_session_factory, entities.finance.id, storage=storage
    )
    for _ in range(2):
        async with db_session_factory() as session, session.begin():
            stored = await session.get(Document, document.id)
            assert stored is not None
            assert stored is not None
            await enqueue_delete(session, stored, settings)

    async with db_session_factory() as session:
        delete_runs = (
            await session.scalars(
                select(ProcessingRun).where(ProcessingRun.job_kind == JOB_DELETE)
            )
        ).all()
    assert len(delete_runs) == 1
    assert delete_runs[0].status == RUN_QUEUED


async def test_a_tombstone_sweep_removes_leaked_artifacts(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """If a delete run is lost, the periodic sweep still cleans the
    artifacts for any document marked deleted."""
    storage = FakeStorage()
    document, _ = await seed_document(
        db_session_factory, entities.finance.id, storage=storage
    )
    storage.objects[
        (
            "context-documents",
            f"workspaces/{entities.finance.id}/documents/{document.id}/runs/x/attempt.json.gz",
        )
    ] = b"{}"
    async with db_session_factory() as session, session.begin():
        stored = await session.get(Document, document.id)
        assert stored is not None
        assert stored is not None
        stored.deleted_at = datetime.now(UTC)

    worker = build_worker(db_session_factory, settings, storage=storage)
    # The sweep runs before the claim, so the leaked artifact is removed
    # even though a run is still waiting to be picked up.
    await worker.run_once()

    assert any(
        prefix == f"workspaces/{entities.finance.id}/documents/{document.id}/"
        for _bucket, prefix in storage.prefixes
    )
    assert (
        "context-documents",
        f"workspaces/{entities.finance.id}/documents/{document.id}/runs/x/attempt.json.gz",
    ) not in storage.objects


async def test_embedding_backfill_requeues_a_run_left_half_indexed(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """A crash between chunking and embedding leaves a ready run with
    un-indexed chunks. The sweep finds it and finishes the job."""
    storage = FakeStorage()
    document, run = await seed_document(
        db_session_factory, entities.finance.id, storage=storage
    )
    vectors = FakeVectorStore()
    indexer = build_worker(
        db_session_factory, settings, storage=storage, vectors=vectors
    )
    await indexer.run_once()

    # rewind to the state a crash would leave
    async with db_session_factory() as session, session.begin():
        for embedding in (
            await session.scalars(
                select(ChunkEmbedding).where(
                    ChunkEmbedding.document_id == document.id
                )
            )
        ).all():
            embedding.status = "pending"
        stored_run = await session.get(ProcessingRun, run.id)
        assert stored_run is not None
        stored_run.embedded_chunk_count = 0

    vectors.points.clear()
    backfill = build_worker(
        db_session_factory, settings, storage=storage, vectors=vectors
    )
    assert await backfill.run_once() is True

    assert len(vectors.points) == 1
    async with db_session_factory() as session:
        embeddings = (
            await session.scalars(
                select(ChunkEmbedding).where(
                    ChunkEmbedding.document_id == document.id
                )
            )
        ).all()
    assert [embedding.status for embedding in embeddings] == [EMBEDDING_INDEXED]


async def test_embedding_backfill_finds_a_run_behind_finished_ones(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """A hundred finished runs must not hide the one that needs fixing.

    The sweep takes the oldest hundred candidates. Fully indexed runs are
    excluded in SQL rather than skipped in the loop: otherwise a backlog of
    finished runs fills the window every interval and whatever sits behind
    them waits forever.
    """
    storage = FakeStorage()
    document, run = await seed_document(
        db_session_factory, entities.finance.id, storage=storage
    )
    vectors = FakeVectorStore()
    indexer = build_worker(
        db_session_factory, settings, storage=storage, vectors=vectors
    )
    await indexer.run_once()

    async with db_session_factory() as session, session.begin():
        for embedding in (
            await session.scalars(
                select(ChunkEmbedding).where(
                    ChunkEmbedding.document_id == document.id
                )
            )
        ).all():
            embedding.status = "pending"
        stored_run = await session.get(ProcessingRun, run.id)
        assert stored_run is not None
        stored_run.embedded_chunk_count = 0

        # Older and already fully indexed: the pre-fix sweep selected these
        # first and stopped at the window, never reaching the run above.
        finished = datetime.now(UTC) - timedelta(days=1)
        for index in range(100):
            finished_run_id = uuid4()
            finished_document_id = uuid4()
            finished_document = Document(
                id=finished_document_id,
                workspace_id=entities.finance.id,
                original_filename=f"finished-{index}.pdf",
                detected_media_type="application/pdf",
                size_bytes=1,
                sha256=hashlib.sha256(f"finished-{index}".encode()).hexdigest(),
                bucket="raw-documents",
                object_key=(
                    f"workspaces/{entities.finance.id}/objects/"
                    f"finished/{finished_document_id}"
                ),
            )
            finished_document.active_processing_run_id = finished_run_id
            session.add(finished_document)
            session.add(
                ProcessingRun(
                    id=finished_run_id,
                    document_id=finished_document_id,
                    workspace_id=entities.finance.id,
                    job_kind=JOB_EXTRACT,
                    pipeline_version="2026.1",
                    status=RUN_READY,
                    chunk_count=1,
                    embedded_chunk_count=1,
                    embedding_profile=settings.embedding_profile,
                    embedding_model=settings.embedding_model,
                    embedding_dimensions=settings.embedding_dimensions,
                    created_at=finished + timedelta(seconds=index),
                )
            )

    vectors.points.clear()
    backfill = build_worker(
        db_session_factory, settings, storage=storage, vectors=vectors
    )
    assert await backfill.run_once() is True

    assert len(vectors.points) == 1
    async with db_session_factory() as session:
        embeddings = (
            await session.scalars(
                select(ChunkEmbedding).where(
                    ChunkEmbedding.document_id == document.id
                )
            )
        ).all()
    assert [embedding.status for embedding in embeddings] == [EMBEDDING_INDEXED]


# -- CSV -----------------------------------------------------------------
#
# A CSV is stored as rows and deliberately never embedded. These tests pin
# that: the columns these files carry are numeric, so a vector per row buys
# noise at 16KB each and finds nothing a filter would not. The load-bearing
# part is the activation gate, which used to reject these documents twice
# over -- rows with no vectors failed `indexed_count != chunk_count`, and a
# run with no chunks at all failed `not chunk_count` -- and reported both as
# a missing embedding, naming neither cause.


async def test_worker_stores_csv_rows_and_never_embeds_them(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    storage = FakeStorage()
    document, run = await seed_document(
        db_session_factory,
        entities.finance.id,
        storage=storage,
        content=csv_bytes(),
        media_type="text/csv",
        filename="sales.csv",
    )
    embeddings = FakeEmbeddingProvider()
    vectors = FakeVectorStore()
    worker = build_worker(
        db_session_factory,
        settings,
        storage=storage,
        embeddings=embeddings,
        vectors=vectors,
    )

    assert await worker.run_once() is True

    async with db_session_factory() as session:
        stored = await session.get(ProcessingRun, run.id)
        assert stored is not None
        rows = (
            await session.execute(
                select(DocumentRow).where(DocumentRow.document_id == document.id)
            )
        ).scalars().all()

    assert stored.status == RUN_READY
    assert stored.row_count == 3
    assert stored.chunk_count == 0
    assert stored.embedded_chunk_count == 0
    # The header is what labels each cell when a query comes back, so it has
    # to be on the run rather than repeated across every row.
    assert stored.csv_columns == ["id", "status", "amount", "currency"]
    assert embeddings.embedded == []
    assert vectors.points == {}

    assert sorted(row.row_number for row in rows) == [1, 2, 3]
    first = next(row for row in rows if row.row_number == 1)
    assert first.data == {
        "id": "1001",
        "status": "OPEN",
        "amount": "1500.00",
        "currency": "USD",
    }


async def test_a_csv_run_completes_while_the_activation_gate_would_have_failed_it(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """No chunks and no vectors is the correct end state, not a failure.

    Pins the observable contract: the document becomes active with rows as
    its only product. Note the CSV path never enters the embedding stage, so
    this is not exercising `_activate_embedded_run` -- that gate carries a
    `run.row_count` branch as depth, and mutating it changes nothing here.
    """
    storage = FakeStorage()
    document, run = await seed_document(
        db_session_factory,
        entities.finance.id,
        storage=storage,
        content=csv_bytes(),
        media_type="text/csv",
        filename="sales.csv",
    )
    worker = build_worker(db_session_factory, settings, storage=storage)

    assert await worker.run_once() is True

    async with db_session_factory() as session:
        stored_document = await session.get(Document, document.id)
        assert stored_document is not None
    assert stored_document is not None
    assert stored_document.active_processing_run_id == run.id
    assert stored_document.deleted_at is None


async def test_a_reingest_removes_the_superseded_rows(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """A re-uploaded CSV must not leave the old version's rows queryable.

    Row-level security is scoped by workspace and nothing else, so there is
    no equivalent of the active-processing-run recheck that keeps superseded
    document chunks out of retrieval results. Without an explicit delete a
    count or sum would silently span both versions of the file.
    """
    storage = FakeStorage()
    document, _ = await seed_document(
        db_session_factory,
        entities.finance.id,
        storage=storage,
        content=csv_bytes(),
        media_type="text/csv",
        filename="sales.csv",
    )
    worker = build_worker(db_session_factory, settings, storage=storage)
    assert await worker.run_once() is True

    # A second run over the same document, as a superseding upload makes.
    second = ProcessingRun(
        id=uuid4(),
        document_id=document.id,
        workspace_id=document.workspace_id,
        job_kind=JOB_EXTRACT,
        pipeline_version="2026.1",
        status=RUN_QUEUED,
    )
    async with db_session_factory() as session, session.begin():
        session.add(second)

    assert await worker.run_once() is True

    async with db_session_factory() as session:
        rows = (
            await session.execute(
                select(DocumentRow).where(DocumentRow.document_id == document.id)
            )
        ).scalars().all()

    assert [row.processing_run_id for row in rows] == [second.id] * 3


async def test_a_retry_of_the_same_run_reuses_row_ids(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """A retried run replaces its rows with identical ids.

    Ids derive from (run_id, row_number), so re-running the same run is
    idempotent. That matters for a turn still holding a source list from
    between the two attempts: the ids it recorded resolve to the same rows
    afterwards. A *new* run gets a new run_id and therefore new ids, which is
    why this is scoped to a retry rather than to a re-upload.
    """
    storage = FakeStorage()
    document, run = await seed_document(
        db_session_factory,
        entities.finance.id,
        storage=storage,
        content=csv_bytes(),
        media_type="text/csv",
        filename="sales.csv",
    )
    worker = build_worker(db_session_factory, settings, storage=storage)
    assert await worker.run_once() is True

    async with db_session_factory() as session:
        before = sorted(
            row.id
            for row in (
                await session.execute(
                    select(DocumentRow).where(DocumentRow.processing_run_id == run.id)
                )
            ).scalars()
        )

    # Put the same run back in the queue, as a retry does.
    async with db_session_factory() as session, session.begin():
        stored = await session.get(ProcessingRun, run.id)
        assert stored is not None
        stored.status = RUN_QUEUED
        stored.lease_token = None

    assert await worker.run_once() is True

    async with db_session_factory() as session:
        after = sorted(
            row.id
            for row in (
                await session.execute(
                    select(DocumentRow).where(DocumentRow.processing_run_id == run.id)
                )
            ).scalars()
        )

    assert after == before
    assert len(after) == 3


async def test_a_semicolon_csv_ingests_with_its_columns_intact(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """The worker must sniff the delimiter the same way validation did.

    If the two disagreed, this file would be one column wide here and two
    columns wide there, and every cell would carry the wrong label.
    """
    storage = FakeStorage()
    document, run = await seed_document(
        db_session_factory,
        entities.finance.id,
        storage=storage,
        content=csv_semicolon_bytes(),
        media_type="text/csv",
        filename="euro.csv",
    )
    worker = build_worker(db_session_factory, settings, storage=storage)

    assert await worker.run_once() is True

    async with db_session_factory() as session:
        stored = await session.get(ProcessingRun, run.id)
        assert stored is not None
        rows = (
            await session.execute(
                select(DocumentRow)
                .where(DocumentRow.document_id == document.id)
                .order_by(DocumentRow.row_number)
            )
        ).scalars().all()

    assert stored.csv_columns == ["id", "status", "amount"]
    assert rows[0].data == {"id": "1", "status": "OPEN", "amount": "10"}


async def test_a_utf8_bom_does_not_become_the_first_column_name(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """Excel writes a BOM; it must not end up glued to the first header."""
    storage = FakeStorage()
    document, run = await seed_document(
        db_session_factory,
        entities.finance.id,
        storage=storage,
        content=csv_with_bom_bytes(),
        media_type="text/csv",
        filename="excel.csv",
    )
    worker = build_worker(db_session_factory, settings, storage=storage)

    assert await worker.run_once() is True

    async with db_session_factory() as session:
        stored = await session.get(ProcessingRun, run.id)
        assert stored is not None
    assert stored.csv_columns == ["id", "status", "amount", "currency"]


async def test_deleting_a_csv_document_removes_its_rows(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """Rows cascade with the document, so no orphan rows outlive a delete."""
    storage = FakeStorage()
    document, _ = await seed_document(
        db_session_factory,
        entities.finance.id,
        storage=storage,
        content=csv_bytes(),
        media_type="text/csv",
        filename="sales.csv",
    )
    worker = build_worker(db_session_factory, settings, storage=storage)
    assert await worker.run_once() is True

    async with db_session_factory() as session, session.begin():
        stored = await session.get(Document, document.id)
        assert stored is not None
        stored.deleted_at = utcnow()
    async with db_session_factory() as session, session.begin():
        session.add(
            ProcessingRun(
                id=uuid4(),
                document_id=document.id,
                workspace_id=document.workspace_id,
                job_kind=JOB_DELETE,
                pipeline_version="2026.1",
                status=RUN_QUEUED,
            )
        )

    assert await worker.run_once() is True

    async with db_session_factory() as session:
        remaining = (
            await session.execute(
                select(DocumentRow).where(DocumentRow.document_id == document.id)
            )
        ).scalars().all()
    assert remaining == []


# --- the vision stage ----------------------------------------------------


def an_extracted_image(ref: str = "#/pictures/0") -> ExtractedImage:
    return ExtractedImage(
        ref=ref,
        page_number=1,
        media_type="image/png",
        data=b"\x89PNG\r\n\x1a\n",
        caption="Figure 1: Net revenue by segment.",
        width=600,
        height=300,
    )


async def test_a_picture_description_is_folded_into_the_chunk_that_held_it(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """A chart's values are in the pixels, so they have to become words.

    The description lands in the chunk the picture was on, and that chunk is
    what gets embedded: a description that was indexed somewhere else would
    answer a different question than the one that was asked.
    """
    storage = FakeStorage()
    document, run = await seed_document(
        db_session_factory, entities.finance.id, storage=storage
    )
    extractor = FakeExtractor(
        chunk_texts=[
            f"Body text.\n{picture_marker('#/pictures/0')}\nMore body text.",
        ],
        images=[an_extracted_image()],
    )
    embeddings = FakeEmbeddingProvider()
    vision = FakeVisionProvider()
    worker = build_worker(
        db_session_factory,
        settings,
        storage=storage,
        extractor=extractor,
        embeddings=embeddings,
        vision=vision,
    )

    assert await worker.run_once() is True

    async with db_session_factory() as session:
        chunks = (
            await session.scalars(
                select(DocumentChunk).where(DocumentChunk.document_id == document.id)
            )
        ).all()
        finished = await session.get(ProcessingRun, run.id)

    assert finished is not None
    assert finished.status == RUN_READY
    assert len(chunks) == 1
    assert "A description of #/pictures/0." in chunks[0].text
    # and the description is what was embedded, not the picture's absence
    assert len(embeddings.embedded) == 1
    assert "A description of #/pictures/0." in embeddings.embedded[0]
    # the model is told where the picture sits, so it can describe it in context
    assert vision.described == ["#/pictures/0"]
    assert "Figure 1: Net revenue by segment." in vision.contexts[0]


async def test_a_failing_model_leaves_the_document_unindexed(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """A document indexed without its figures is a document that looks
    complete and is not, and nothing downstream can tell the difference.

    So the run is handed back for another attempt rather than finished with
    the pictures missing.
    """
    storage = FakeStorage()
    document, run = await seed_document(
        db_session_factory, entities.finance.id, storage=storage
    )
    extractor = FakeExtractor(
        chunk_texts=[f"Body text.\n{picture_marker('#/pictures/0')}"],
        images=[an_extracted_image()],
    )
    embeddings = FakeEmbeddingProvider()
    worker = build_worker(
        db_session_factory,
        settings,
        storage=storage,
        extractor=extractor,
        embeddings=embeddings,
        vision=FakeVisionProvider(VisionProviderError("the model is down", retryable=True)),
    )

    assert await worker.run_once() is True

    async with db_session_factory() as session:
        chunks = (
            await session.scalars(
                select(DocumentChunk).where(DocumentChunk.document_id == document.id)
            )
        ).all()
        finished = await session.get(ProcessingRun, run.id)

    assert finished is not None
    assert finished.status == RUN_RETRY_WAIT
    assert chunks == []
    assert embeddings.embedded == []


async def test_a_rejected_model_key_fails_the_run_outright(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """A key the endpoint refuses is a mistake, not a hiccup.

    Retrying it three times would only delay the report that the
    deployment is misconfigured.
    """
    storage = FakeStorage()
    document, run = await seed_document(
        db_session_factory, entities.finance.id, storage=storage
    )
    extractor = FakeExtractor(
        chunk_texts=[f"Body text.\n{picture_marker('#/pictures/0')}"],
        images=[an_extracted_image()],
    )
    worker = build_worker(
        db_session_factory,
        settings,
        storage=storage,
        extractor=extractor,
        vision=FakeVisionProvider(
            VisionProviderError("the key was refused", retryable=False)
        ),
    )

    assert await worker.run_once() is True

    async with db_session_factory() as session:
        finished = await session.get(ProcessingRun, run.id)

    assert finished is not None
    assert finished.status == RUN_FAILED
    assert "the key was refused" in (finished.last_error or "")


async def test_the_picture_stage_is_skipped_when_it_is_switched_off(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """With the stage off, the document is indexed exactly as it was before
    the stage existed: no picture is described and no model is called.
    """
    off = settings.model_copy(update={"image_processing_enabled": False})
    storage = FakeStorage()
    document, run = await seed_document(
        db_session_factory, entities.finance.id, storage=storage
    )
    extractor = FakeExtractor(
        chunk_texts=[f"Body text.\n{picture_marker('#/pictures/0')}"],
        images=[an_extracted_image()],
    )
    vision = FakeVisionProvider()
    worker = build_worker(
        db_session_factory,
        off,
        storage=storage,
        extractor=extractor,
        vision=vision,
    )

    assert await worker.run_once() is True

    async with db_session_factory() as session:
        finished = await session.get(ProcessingRun, run.id)
        chunks = (
            await session.scalars(
                select(DocumentChunk).where(DocumentChunk.document_id == document.id)
            )
        ).all()

    assert finished is not None
    assert finished.status == RUN_READY
    assert vision.described == []
    # and the marker never reaches the text that gets embedded
    assert "PICTURE" not in chunks[0].text
    assert chunks[0].text == "Body text.\n[Image]"


async def test_the_manifest_says_how_many_pictures_were_described(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    storage = FakeStorage()
    document, run = await seed_document(
        db_session_factory, entities.finance.id, storage=storage
    )
    extractor = FakeExtractor(
        chunk_texts=[f"Body text.\n{picture_marker('#/pictures/0')}"],
        images=[an_extracted_image()],
    )
    worker = build_worker(
        db_session_factory,
        settings,
        storage=storage,
        extractor=extractor,
        vision=FakeVisionProvider(),
    )

    assert await worker.run_once() is True

    manifest = next(
        data
        for (bucket, key), data in storage.objects.items()
        if key.endswith("manifest.json")
    )
    payload = json.loads(manifest)

    assert payload["vision"]["enabled"] is True
    assert payload["vision"]["images_found"] == 1
    assert payload["vision"]["images_described"] == 1
    assert payload["vision"]["model"] == settings.vlm_model


async def test_zero_max_images_means_no_cap(
    db_session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    entities: SeededEntities,
) -> None:
    """Zero is how the extractor spells "no cap".

    The worker has to read it the same way. Slicing the list at zero would
    throw away every picture the extractor collected, and the document would
    be indexed with ``[Image]`` lines where its figures were.
    """
    no_cap = settings.model_copy(update={"vlm_max_images_per_document": 0})
    storage = FakeStorage()
    document, run = await seed_document(
        db_session_factory, entities.finance.id, storage=storage
    )
    extractor = FakeExtractor(
        chunk_texts=[
            f"First figure.\n{picture_marker('#/pictures/0')}\n"
            f"Second figure.\n{picture_marker('#/pictures/1')}"
        ],
        images=[
            an_extracted_image("#/pictures/0"),
            an_extracted_image("#/pictures/1"),
        ],
    )
    vision = FakeVisionProvider()
    worker = build_worker(
        db_session_factory,
        no_cap,
        storage=storage,
        extractor=extractor,
        vision=vision,
    )

    assert await worker.run_once() is True

    async with db_session_factory() as session:
        finished = await session.get(ProcessingRun, run.id)
        chunks = (
            await session.scalars(
                select(DocumentChunk).where(DocumentChunk.document_id == document.id)
            )
        ).all()

    assert finished is not None
    assert finished.status == RUN_READY
    assert vision.described == ["#/pictures/0", "#/pictures/1"]
    assert "A description of #/pictures/0." in chunks[0].text
    assert "A description of #/pictures/1." in chunks[0].text
    assert "[Image]" not in chunks[0].text
