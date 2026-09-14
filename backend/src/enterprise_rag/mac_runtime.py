"""Real-provider, single-process composition root for a local macOS demo."""

import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import cast

from fastapi import FastAPI

from enterprise_rag.adapters.cleaners import DeterministicCleaner
from enterprise_rag.adapters.database import Database
from enterprise_rag.adapters.database.jobs import IngestionJobRepository
from enterprise_rag.adapters.embeddings import (
    LocalMultilingualEmbedding,
    OpenAICompatibleEmbedding,
)
from enterprise_rag.adapters.embeddings.fastembed_local import DEFAULT_MODEL as EMBEDDING_MODEL
from enterprise_rag.adapters.llms import OpenAICompatibleLanguageModel
from enterprise_rag.adapters.loaders import PdfLoader, SpreadsheetLoader, TextDocumentLoader
from enterprise_rag.adapters.object_store import LocalObjectStore
from enterprise_rag.adapters.ocr import TesseractOcrEngine
from enterprise_rag.adapters.planners import LanguageModelQueryPlanner
from enterprise_rag.adapters.rerankers import (
    LocalFastEmbedReranker,
    OpenAICompatibleReranker,
)
from enterprise_rag.adapters.sparse import HashingSparseEncoder
from enterprise_rag.adapters.splitters import StructureAwareSplitter
from enterprise_rag.adapters.vector_store import MilvusLiteVectorStore
from enterprise_rag.adapters.vision import NoopVisionProvider
from enterprise_rag.api import create_app
from enterprise_rag.config import AppSettings, load_settings
from enterprise_rag.observability import configure_json_logging
from enterprise_rag.ports import Provider, ProviderRegistry
from enterprise_rag.services import (
    BoundedLanguageModel,
    DeterministicEvaluator,
    ImageEnricher,
    IngestionPipeline,
    ManualLlmCleaningService,
    ProjectionService,
    ProviderReindexService,
    QueryPlanningService,
    SemanticQueryRunner,
    build_persistent_tracing,
)
from enterprise_rag.services.provider_catalog import (
    SILICONFLOW_BASE_URL,
    SILICONFLOW_EMBEDDING_MODEL,
    SILICONFLOW_RERANKER_MODEL,
    RuntimeProviderCatalog,
    load_provider_selection,
    selected_embedding_dimension,
    selected_runtime_model,
)

LOGGER = logging.getLogger(__name__)
DEFAULT_RERANKER_MODEL = "jinaai/jina-reranker-v2-base-multilingual"


def build_mac_runtime_app(settings: AppSettings | None = None) -> FastAPI:
    """Compose upload, parsing, semantic retrieval, reranking, and remote generation."""

    active = settings or load_settings()
    credentials = active.credentials
    if credentials.database_url is None:
        raise RuntimeError("DATABASE_URL is required by the Mac runtime")
    if credentials.session_secret is None:
        raise RuntimeError("SESSION_SECRET is required by the Mac runtime")
    if credentials.llm_base_url is None:
        raise RuntimeError("LLM_BASE_URL is required by the Mac runtime")
    if credentials.llm_api_key is None:
        raise RuntimeError("LLM_API_KEY is required by the Mac runtime")
    if credentials.llm_model is None or not credentials.llm_model.strip():
        raise RuntimeError("LLM_MODEL is required by the Mac runtime")
    expected = {
        "llm": "openai_compatible",
        "embedding": "local_multilingual_minilm",
        "reranker": "local_cross_encoder",
        "vector_store": "milvus_lite",
    }
    selected = active.providers.model_dump()
    mismatched = [name for name, value in expected.items() if selected[name] != value]
    if mismatched:
        raise RuntimeError(
            "The Mac runtime requires these provider selections: " + ", ".join(mismatched)
        )

    runtime_root = active.ingestion.object_store_root.resolve()
    model_cache = runtime_root.parent / "model-cache"
    selection_path = runtime_root.parent / "provider-selection.json"
    selection = load_provider_selection(selection_path)
    embedding_model = selected_runtime_model(
        selection,
        kind="embedding",
        default=credentials.embedding_model or EMBEDDING_MODEL,
    )
    reranker_model = selected_runtime_model(
        selection,
        kind="reranker",
        default=credentials.rerank_model or DEFAULT_RERANKER_MODEL,
    )
    llm_model = selected_runtime_model(
        selection,
        kind="llm",
        default=credentials.llm_model or "",
    )
    index_revision = _index_revision(embedding_model)

    shared_remote_key = (
        credentials.siliconflow_api_key.get_secret_value()
        if credentials.siliconflow_api_key is not None
        else None
    )
    embedding_remote_key = (
        credentials.embedding_api_key.get_secret_value()
        if credentials.embedding_api_key is not None
        else shared_remote_key
    )
    reranker_remote_key = (
        credentials.rerank_api_key.get_secret_value()
        if credentials.rerank_api_key is not None
        else shared_remote_key
    )
    shared_remote_base_url = str(credentials.siliconflow_base_url or SILICONFLOW_BASE_URL)

    database = Database(credentials.database_url.get_secret_value())
    object_store = LocalObjectStore(runtime_root)
    configured_embedding_dimension = selected_embedding_dimension(
        selection, active.ingestion.embedding_dimension
    )
    embedding: LocalMultilingualEmbedding | OpenAICompatibleEmbedding
    if embedding_model == SILICONFLOW_EMBEDDING_MODEL:
        if not embedding_remote_key:
            raise RuntimeError(
                "SILICONFLOW_API_KEY or EMBEDDING_API_KEY is required for BAAI/bge-m3"
            )
        embedding = OpenAICompatibleEmbedding(
            base_url=str(credentials.embedding_base_url or shared_remote_base_url),
            api_key=embedding_remote_key,
            model=embedding_model,
            provider_name="siliconflow",
            dimension=1024,
            input_token_limit=8192,
            tokenizer_name=f"estimated-tokenizer:{embedding_model}",
            batch_size=active.ingestion.embedding_batch_size,
            max_batch_tokens=active.ingestion.embedding_batch_tokens,
            timeout_seconds=active.cost_guard.provider_timeout_seconds,
            max_retries=active.cost_guard.provider_max_retries,
        )
    else:
        embedding = LocalMultilingualEmbedding(
            model_name=embedding_model,
            cache_dir=model_cache / "embedding",
            batch_size=active.ingestion.embedding_batch_size,
            max_batch_tokens=active.ingestion.embedding_batch_tokens,
        )
        embedding.warm_tokenizer()
    if embedding.dimension != configured_embedding_dimension:
        raise RuntimeError(
            "Configured ingestion.embedding_dimension does not match the selected embedding model"
        )
    sparse = HashingSparseEncoder()
    vector_store = MilvusLiteVectorStore(runtime_root.parent / "milvus" / "vectors.db")
    reranker: LocalFastEmbedReranker | OpenAICompatibleReranker
    if reranker_model == SILICONFLOW_RERANKER_MODEL:
        if not reranker_remote_key:
            raise RuntimeError(
                "SILICONFLOW_API_KEY or RERANK_API_KEY is required for BAAI/bge-reranker-v2-m3"
            )
        reranker = OpenAICompatibleReranker(
            base_url=str(credentials.rerank_base_url or shared_remote_base_url),
            api_key=reranker_remote_key,
            model=reranker_model,
            provider_name="siliconflow",
            timeout_seconds=active.cost_guard.provider_timeout_seconds,
            max_retries=active.cost_guard.provider_max_retries,
        )
    else:
        reranker = LocalFastEmbedReranker(
            model_name=reranker_model,
            cache_dir=model_cache / "reranker",
        )
    raw_language_model = OpenAICompatibleLanguageModel(
        base_url=str(credentials.llm_base_url),
        api_key=credentials.llm_api_key.get_secret_value(),
        model=llm_model,
        timeout_seconds=active.cost_guard.provider_timeout_seconds,
    )
    language_model = BoundedLanguageModel(
        raw_language_model,
        timeout_seconds=active.cost_guard.provider_timeout_seconds,
        max_retries=active.cost_guard.provider_max_retries,
        retry_backoff_seconds=active.cost_guard.provider_retry_backoff_seconds,
    )
    query_planner = QueryPlanningService(LanguageModelQueryPlanner(language_model))
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
        # Root recovery supplies context; Mac demo Leaves are intentionally disjoint.
        overlap_tokens=0,
        token_counter=embedding.count_tokens,
        tokenizer=embedding.tokenizer_name,
        embedding_token_limit=embedding.input_token_limit,
    )
    evaluator = DeterministicEvaluator()
    registry = ProviderRegistry()
    for provider in (
        embedding,
        sparse,
        vector_store,
        reranker,
        language_model,
        splitter,
    ):
        registry.register(provider)
    registry.register(cast(Provider, evaluator))
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
        temporary_root=runtime_root.parent / "ingestion-temporary",
        index_revision=index_revision,
        lease_for=timedelta(minutes=2),
        retry_delay=timedelta(seconds=1),
        tracer_provider=tracer_provider,
        trace_recorder=trace_service,
    )
    retrieval = active.retrieval
    query_runner = SemanticQueryRunner(
        database=database,
        embedding=embedding,
        sparse=sparse,
        vector_store=vector_store,
        reranker=reranker,
        language_model=language_model,
        index_revision=index_revision,
        dense_top_k=retrieval.dense_candidates,
        sparse_top_k=retrieval.sparse_candidates,
        fused_top_k=retrieval.fused_candidates,
        rerank_candidates=retrieval.rerank_candidates,
        selected_leaf_k=retrieval.selected_leaf_k,
        rrf_k=retrieval.rrf_k,
        max_parent_chars=retrieval.max_parent_chars,
        planner=query_planner,
    )
    manual_llm_cleaning = ManualLlmCleaningService(
        database=database,
        language_model=language_model,
        splitter=splitter,
        projection=projection,
        vector_store=vector_store,
        temporary_root=runtime_root.parent / "llm-cleaning-temporary",
    )
    provider_catalog = RuntimeProviderCatalog(
        registry=registry,
        selection_path=selection_path,
        current_models={
            "embedding": embedding_model,
            "reranker": reranker_model,
            "llm": llm_model,
        },
        current_embedding_dimension=embedding.dimension,
        current_embedding_input_token_limit=embedding.input_token_limit,
        remote_credentials=frozenset(
            kind
            for kind, configured in (
                ("embedding", embedding_remote_key),
                ("reranker", reranker_remote_key),
            )
            if configured
        ),
    )
    provider_reindex = ProviderReindexService(
        database=database,
        splitter=splitter,
        projection=projection,
        vector_store=vector_store,
        active_revision=index_revision,
        embedding_model=embedding_model,
        embedding_dimension=embedding.dimension,
        temporary_root=runtime_root.parent / "provider-reindex-temporary",
        max_documents=active.security.anonymous_max_ready_documents,
    )

    async def worker() -> None:
        loop = asyncio.get_running_loop()
        next_recovery = 0.0
        while True:
            try:
                now = loop.time()
                if now >= next_recovery:
                    async with database.session() as session:
                        await IngestionJobRepository(session).recover_expired(
                            now=datetime.now(UTC), limit=10
                        )
                    next_recovery = now + 5.0
                result = await pipeline.run_once(owner="mac-local-worker")
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception(
                    "rag.mac_worker.poll_failed",
                    extra={"event_code": "MAC_WORKER_POLL_FAILED", "outcome": "error"},
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
        vision.aclose,
        ocr.aclose,
        cleaner.aclose,
        *(loader.aclose for loader in loaders),
        registry.aclose,
        close_tracer,
    )
    return create_app(
        active,
        database=database,
        object_store=object_store,
        query_runner=query_runner,
        tracer_provider=tracer_provider,
        trace_service=trace_service,
        provider_registry=registry,
        manual_llm_cleaning_service=manual_llm_cleaning,
        provider_catalog=provider_catalog,
        provider_reindex=provider_reindex,
        background_tasks=(worker,),
        resource_closers=closers,
    )


def _index_revision(embedding_model: str) -> str:
    digest = hashlib.sha256(embedding_model.encode("utf-8")).hexdigest()[:12]
    return f"mac-semantic-{digest}"


settings = load_settings()
configure_json_logging(environment=settings.app.environment)
app = build_mac_runtime_app(settings)
