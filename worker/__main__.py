import asyncio
import ctypes
import logging
import os
import signal
import sys
from contextlib import suppress

from app.core.config import get_settings
from app.db.session import session_factory
from app.embeddings.openai_compatible import OpenAICompatibleEmbeddingProvider
from app.embeddings.qdrant import QdrantVectorStore
from app.extraction.sandbox import SandboxedDoclingExtractor
from app.infrastructure.storage import get_artifact_storage
from app.processing.worker import ProcessingWorker


async def run_worker() -> None:
    harden_worker_process()
    settings = get_settings()
    embedding_provider = OpenAICompatibleEmbeddingProvider(
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key.get_secret_value(),
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        timeout_seconds=settings.embedding_timeout_seconds,
    )
    vector_store = QdrantVectorStore(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key.get_secret_value(),
        collection=settings.qdrant_collection,
        dimensions=settings.embedding_dimensions,
        timeout_seconds=settings.qdrant_timeout_seconds,
        vectors_on_disk=settings.qdrant_vectors_on_disk,
    )
    worker = ProcessingWorker(
        session_factory,
        get_artifact_storage(),
        SandboxedDoclingExtractor(
            ocr_languages=settings.ocr_languages,
            chunk_max_tokens=settings.chunk_max_tokens,
            tokenizer_name=settings.chunk_tokenizer,
            artifacts_path=settings.docling_artifacts_path,
            timeout_seconds=settings.processing_timeout_seconds,
            memory_limit_bytes=settings.parser_memory_limit_bytes,
            max_processes=settings.parser_max_processes,
            max_artifact_bytes=settings.max_artifact_bytes,
            max_chunk_payload_bytes=settings.max_chunk_payload_bytes,
            max_extracted_text_bytes=settings.max_extracted_text_bytes,
            max_chunks=settings.max_chunks_per_document,
        ),
        settings,
        embedding_provider,
        vector_store,
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(stop_signal, stop_event.set)
    try:
        await worker.run(stop_event)
    finally:
        await embedding_provider.aclose()
        await vector_store.aclose()


def harden_worker_process() -> None:
    os.umask(0o077)
    if not sys.platform.startswith("linux"):
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(4, 0, 0, 0, 0) != 0:  # PR_SET_DUMPABLE
        error_number = ctypes.get_errno()
        raise OSError(error_number, "Could not disable worker process dumping")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
