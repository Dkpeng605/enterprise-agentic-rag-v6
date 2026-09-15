"""Standalone ingestion Worker composition for production deployments."""

import asyncio
import logging
import os
import signal
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from enterprise_rag.adapters.cleaners import DeterministicCleaner
from enterprise_rag.adapters.database import Database
from enterprise_rag.adapters.database.jobs import IngestionJobRepository
from enterprise_rag.adapters.embeddings import (
    DEFAULT_MODEL,
    LocalMultilingualEmbedding,
    OpenAICompatibleEmbedding,
)
from enterprise_rag.adapters.loaders import PdfLoader, SpreadsheetLoader, TextDocumentLoader
from enterprise_rag.adapters.object_store import LocalObjectStore
from enterprise_rag.adapters.ocr import TesseractOcrEngine
from enterprise_rag.adapters.sparse import HashingSparseEncoder, MilvusBuiltinBm25Encoder
from enterprise_rag.adapters.splitters import StructureAwareSplitter
from enterprise_rag.adapters.vector_store import MilvusLiteVectorStore, MilvusRemoteVectorStore
from enterprise_rag.config import AppSettings, load_settings
from enterprise_rag.observability import configure_json_logging
from enterprise_rag.ports.loader import Loader
from enterprise_rag.services import (
    ImageEnricher,
    IngestionPipeline,
    IngestionWorker,
    ProjectionService,
)
from enterprise_rag.services.index_revision import index_revision
from enterprise_rag.services.traces import build_persistent_tracing
from enterprise_rag.services.vision_provider import build_vision_provider

LOGGER = logging.getLogger(__name__)
Closer = Callable[[], Awaitable[None]]
EmbeddingRuntime = LocalMultilingualEmbedding | OpenAICompatibleEmbedding
SparseRuntime = HashingSparseEncoder | MilvusBuiltinBm25Encoder
VectorRuntime = MilvusLiteVectorStore | MilvusRemoteVectorStore


@dataclass(slots=True)
class ProductionWorkerRuntime:
    """Resources owned by one standalone Worker process."""

    worker: IngestionWorker
    index_revision: str
    _closers: tuple[Closer, ...]
    _closed: bool = False

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await _close_resources(self._closers, raise_errors=True)


async def build_production_worker(
    settings: AppSettings | None = None,
) -> ProductionWorkerRuntime:
    """Compose the real ingestion pipeline without starting an HTTP server.

    PostgreSQL leases coordinate Job ownership. A production process must use a
    server-backed Milvus deployment; Milvus Lite is accepted only for a local
    foreground Worker while the API is stopped.
    """

    active = settings or load_settings()
    credentials = active.credentials
    if credentials.database_url is None:
        raise RuntimeError("DATABASE_URL is required by the Worker")

    runtime_root = active.ingestion.object_store_root.resolve()
    closers: list[Closer] = []
    try:
        database = Database(credentials.database_url.get_secret_value())
        closers.append(database.dispose)
        object_store = LocalObjectStore(runtime_root)
        closers.append(object_store.aclose)
        embedding, embedding_model = _build_embedding(active)
        closers.append(embedding.aclose)
        sparse = _build_sparse(active)
        closers.append(sparse.aclose)
        vector_store = _build_vector_store(active)
        closers.append(vector_store.aclose)
        vision = build_vision_provider(active)
        closers.append(vision.aclose)
        ocr = TesseractOcrEngine(languages=active.ingestion.pdf_ocr_languages)
        closers.append(ocr.aclose)

        loaders_list: list[Loader] = []
        pdf_loader = PdfLoader(
            ocr,
            ocr_min_chars=active.ingestion.pdf_ocr_min_chars,
            render_scale=active.ingestion.pdf_render_scale,
        )
        loaders_list.append(pdf_loader)
        closers.append(pdf_loader.aclose)
        spreadsheet_loader = SpreadsheetLoader(
            rows_per_root=active.ingestion.spreadsheet_rows_per_root,
            csv_fallback_encoding=active.ingestion.csv_fallback_encoding,
        )
        loaders_list.append(spreadsheet_loader)
        closers.append(spreadsheet_loader.aclose)
        text_loader = TextDocumentLoader()
        loaders_list.append(text_loader)
        closers.append(text_loader.aclose)
        loaders = tuple(loaders_list)
        cleaner = DeterministicCleaner()
        closers.append(cleaner.aclose)
        splitter = StructureAwareSplitter(
            target_tokens=active.ingestion.target_tokens,
            max_tokens=active.ingestion.max_tokens,
            overlap_tokens=active.ingestion.overlap_tokens,
            token_counter=embedding.count_tokens,
            tokenizer=embedding.tokenizer_name,
            embedding_token_limit=embedding.input_token_limit,
        )
        closers.append(splitter.aclose)
        sparse_info = sparse.info()
        revision = index_revision(
            embedding_model,
            sparse_provider=sparse_info.name,
            sparse_version=sparse_info.version,
        )
        projection = ProjectionService(
            embedding=embedding,
            sparse=sparse,
            vector_store=vector_store,
            batch_size=active.ingestion.embedding_batch_size,
        )
        tracer_provider, trace_service = build_persistent_tracing(database)

        async def close_tracer() -> None:
            await asyncio.to_thread(tracer_provider.shutdown)

        closers.append(close_tracer)
        pipeline = IngestionPipeline(
            database=database,
            object_store=object_store,
            loaders=loaders,
            cleaner=cleaner,
            splitter=splitter,
            image_enricher=ImageEnricher(object_store, vision),
            projection=projection,
            vector_store=vector_store,
            temporary_root=runtime_root.parent / "ingestion-temporary",
            index_revision=revision,
            lease_for=timedelta(seconds=active.worker.lease_seconds),
            retry_delay=timedelta(seconds=1),
            tracer_provider=tracer_provider,
            trace_recorder=trace_service,
        )

        async def recover_expired(now: datetime, limit: int) -> int:
            async with database.session() as session:
                retry_count, failed_count = await IngestionJobRepository(
                    session
                ).recover_expired(now=now, limit=limit)
            return retry_count + failed_count

        worker = IngestionWorker(
            pipeline,
            recover_expired,
            owner=_worker_owner(),
            poll_interval_seconds=active.worker.poll_interval_seconds,
            recovery_interval_seconds=active.worker.recovery_interval_seconds,
            recovery_limit=active.worker.recovery_limit,
        )
        return ProductionWorkerRuntime(
            worker=worker,
            index_revision=revision,
            _closers=tuple(closers),
        )
    except BaseException:
        await _close_resources(tuple(closers), raise_errors=False)
        raise


async def _close_resources(closers: tuple[Closer, ...], *, raise_errors: bool) -> None:
    """Close every acquired resource, preserving the first error if requested."""

    first_error: BaseException | None = None
    for closer in reversed(closers):
        try:
            await closer()
        except asyncio.CancelledError as error:
            if first_error is None:
                first_error = error
        except Exception as error:
            LOGGER.warning(
                "rag.worker.resource_close_failed",
                extra={
                    "event_code": "WORKER_RESOURCE_CLOSE_FAILED",
                    "outcome": "degraded",
                },
            )
            if first_error is None:
                first_error = error
    if raise_errors and first_error is not None:
        raise first_error


def _build_embedding(active: AppSettings) -> tuple[EmbeddingRuntime, str]:
    credentials = active.credentials
    if active.app.environment == "production" and active.providers.embedding != "openai_compatible":
        raise RuntimeError("The production Worker requires an OpenAI-compatible Embedding Provider")
    if active.providers.embedding == "openai_compatible":
        if (
            credentials.embedding_base_url is None
            or credentials.embedding_api_key is None
            or credentials.embedding_model is None
            or not credentials.embedding_model.strip()
        ):
            raise RuntimeError(
                "EMBEDDING_BASE_URL, EMBEDDING_API_KEY, and EMBEDDING_MODEL are required"
            )
        model = credentials.embedding_model
        return (
            OpenAICompatibleEmbedding(
                base_url=str(credentials.embedding_base_url),
                api_key=credentials.embedding_api_key.get_secret_value(),
                model=model,
                dimension=active.ingestion.embedding_dimension,
                batch_size=active.ingestion.embedding_batch_size,
                max_batch_tokens=active.ingestion.embedding_batch_tokens,
                timeout_seconds=active.ingestion.embedding_timeout_seconds,
                max_retries=active.ingestion.embedding_max_retries,
            ),
            model,
        )
    if active.providers.embedding != "local_multilingual_minilm":
        raise RuntimeError(f"Unsupported Worker Embedding Provider: {active.providers.embedding}")
    model = credentials.embedding_model or DEFAULT_MODEL
    provider = LocalMultilingualEmbedding(
        model_name=model,
        cache_dir=active.ingestion.object_store_root.resolve().parent / "model-cache" / "embedding",
        batch_size=active.ingestion.embedding_batch_size,
        max_batch_tokens=active.ingestion.embedding_batch_tokens,
        dimension=active.ingestion.embedding_dimension,
    )
    provider.warm_tokenizer()
    return provider, model


def _build_sparse(active: AppSettings) -> SparseRuntime:
    if active.providers.sparse_encoder == "hashing_lexical":
        return HashingSparseEncoder()
    if active.providers.sparse_encoder == "milvus_builtin_bm25":
        return MilvusBuiltinBm25Encoder()
    raise RuntimeError(f"Unsupported Worker Sparse Provider: {active.providers.sparse_encoder}")


def _build_vector_store(active: AppSettings) -> VectorRuntime:
    credentials = active.credentials
    if active.providers.vector_store == "milvus_remote":
        if credentials.vector_store_uri is None or credentials.vector_store_token is None:
            raise RuntimeError("VECTOR_STORE_URI and VECTOR_STORE_TOKEN are required")
        return MilvusRemoteVectorStore(
            str(credentials.vector_store_uri),
            token=credentials.vector_store_token.get_secret_value(),
            db_name=credentials.vector_store_database,
        )
    if active.app.environment == "production":
        raise RuntimeError("Production Worker cannot use single-process Milvus Lite")
    if active.providers.vector_store == "milvus_lite":
        return MilvusLiteVectorStore(runtime_path(active) / "milvus" / "vectors.db")
    raise RuntimeError(f"Unsupported Worker VectorStore: {active.providers.vector_store}")


def runtime_path(active: AppSettings) -> Path:
    return active.ingestion.object_store_root.resolve().parent


def _worker_owner() -> str:
    configured = os.environ.get("WORKER_ID", "").strip()
    if configured:
        return configured[:200]
    hostname = socket.gethostname().replace("/", "-")[:120]
    return f"enterprise-rag-worker:{hostname}:{os.getpid()}"


async def run_worker(settings: AppSettings | None = None) -> None:
    runtime = await build_production_worker(settings)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError):
            LOGGER.warning(
                "rag.worker.signal_handler_unavailable",
                extra={
                    "event_code": "WORKER_SIGNAL_HANDLER_UNAVAILABLE",
                    "outcome": "degraded",
                },
            )
    try:
        LOGGER.info(
            "rag.worker.started",
            extra={
                "event_code": "WORKER_STARTED",
                "outcome": "ok",
                "index_revision": runtime.index_revision,
                "owner": runtime.worker.owner,
            },
        )
        await runtime.worker.run_forever(stop_event)
    finally:
        await runtime.aclose()
        LOGGER.info(
            "rag.worker.stopped",
            extra={"event_code": "WORKER_STOPPED", "outcome": "ok"},
        )


def main() -> int:
    settings = load_settings()
    configure_json_logging(environment=settings.app.environment)
    asyncio.run(run_worker(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ProductionWorkerRuntime", "build_production_worker", "run_worker"]
