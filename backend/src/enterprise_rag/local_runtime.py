"""Explicit offline composition root used by Compose browser acceptance."""

import asyncio
import logging
from datetime import timedelta

from fastapi import FastAPI

from enterprise_rag.adapters.cleaners import DeterministicCleaner
from enterprise_rag.adapters.database import Database
from enterprise_rag.adapters.embeddings import HashingDenseEmbedding
from enterprise_rag.adapters.loaders import PdfLoader, SpreadsheetLoader, TextDocumentLoader
from enterprise_rag.adapters.object_store import LocalObjectStore
from enterprise_rag.adapters.ocr import TesseractOcrEngine
from enterprise_rag.adapters.sparse import HashingSparseEncoder
from enterprise_rag.adapters.splitters import StructureAwareSplitter
from enterprise_rag.adapters.vector_store import MilvusLiteVectorStore
from enterprise_rag.adapters.vision import NoopVisionProvider
from enterprise_rag.api import create_app
from enterprise_rag.config import AppSettings, load_settings
from enterprise_rag.mcp.catalog import McpCapabilityCatalog
from enterprise_rag.observability import configure_json_logging
from enterprise_rag.services import (
    DeterministicLocalQueryRunner,
    ImageEnricher,
    IngestionPipeline,
    ProjectionService,
    build_persistent_tracing,
)

LOGGER = logging.getLogger(__name__)
INDEX_REVISION = "local-e2e-hashing-v1"


def build_local_runtime_app(settings: AppSettings | None = None) -> FastAPI:
    """Compose one single-process, no-network runtime for repeatable browser tests."""

    active = settings or load_settings()
    if active.credentials.database_url is None:
        raise RuntimeError("DATABASE_URL is required by the local E2E runtime")
    if active.credentials.session_secret is None:
        raise RuntimeError("SESSION_SECRET is required by the local E2E runtime")

    runtime_root = active.ingestion.object_store_root.resolve()
    database = Database(active.credentials.database_url.get_secret_value())
    object_store = LocalObjectStore(runtime_root)
    embedding = HashingDenseEmbedding(dimension=active.ingestion.embedding_dimension)
    sparse = HashingSparseEncoder()
    vector_store = MilvusLiteVectorStore(runtime_root / "milvus" / "vectors.db")
    ocr = TesseractOcrEngine(languages=active.ingestion.pdf_ocr_languages)
    vision = NoopVisionProvider()
    loaders = (
        PdfLoader(
            ocr,
            ocr_min_chars=active.ingestion.pdf_ocr_min_chars,
            render_scale=active.ingestion.pdf_render_scale,
        ),
        SpreadsheetLoader(
            rows_per_root=active.ingestion.spreadsheet_rows_per_root,
            csv_fallback_encoding=active.ingestion.csv_fallback_encoding,
        ),
        TextDocumentLoader(),
    )
    cleaner = DeterministicCleaner()
    splitter = StructureAwareSplitter(
        target_tokens=active.ingestion.target_tokens,
        max_tokens=active.ingestion.max_tokens,
        overlap_tokens=active.ingestion.overlap_tokens,
    )
    projection = ProjectionService(
        embedding=embedding,
        sparse=sparse,
        vector_store=vector_store,
        batch_size=active.ingestion.embedding_batch_size,
    )
    tracer_provider, trace_service = build_persistent_tracing(database)
    pipeline = IngestionPipeline(
        database=database,
        object_store=object_store,
        loaders=loaders,
        cleaner=cleaner,
        splitter=splitter,
        image_enricher=ImageEnricher(object_store, vision),
        projection=projection,
        vector_store=vector_store,
        temporary_root=runtime_root / "ingestion-temporary",
        index_revision=INDEX_REVISION,
        lease_for=timedelta(minutes=2),
        retry_delay=timedelta(seconds=1),
        tracer_provider=tracer_provider,
        trace_recorder=trace_service,
    )
    retrieval = active.retrieval
    query_runner = DeterministicLocalQueryRunner(
        database=database,
        embedding=embedding,
        sparse=sparse,
        vector_store=vector_store,
        index_revision=INDEX_REVISION,
        dense_top_k=retrieval.dense_candidates,
        sparse_top_k=retrieval.sparse_candidates,
        fused_top_k=retrieval.fused_candidates,
        rerank_candidates=retrieval.rerank_candidates,
        selected_leaf_k=retrieval.selected_leaf_k,
        rrf_k=retrieval.rrf_k,
        max_parent_chars=retrieval.max_parent_chars,
    )

    async def worker() -> None:
        while True:
            try:
                result = await pipeline.run_once(owner="compose-local-worker")
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception(
                    "rag.local_worker.poll_failed",
                    extra={"event_code": "LOCAL_WORKER_POLL_FAILED", "outcome": "error"},
                )
                await asyncio.sleep(1)
                continue
            if result is None:
                await asyncio.sleep(0.2)

    async def close_tracer() -> None:
        await asyncio.to_thread(tracer_provider.shutdown)

    closers = (
        database.dispose,
        object_store.aclose,
        vector_store.aclose,
        embedding.aclose,
        sparse.aclose,
        vision.aclose,
        ocr.aclose,
        splitter.aclose,
        cleaner.aclose,
        *(loader.aclose for loader in loaders),
        close_tracer,
    )
    return create_app(
        active,
        database=database,
        object_store=object_store,
        query_runner=query_runner,
        tracer_provider=tracer_provider,
        trace_service=trace_service,
        mcp_catalog=McpCapabilityCatalog.from_environment(),
        background_tasks=(worker,),
        resource_closers=closers,
    )


settings = load_settings()
configure_json_logging(environment=settings.app.environment)
app = build_local_runtime_app(settings)
