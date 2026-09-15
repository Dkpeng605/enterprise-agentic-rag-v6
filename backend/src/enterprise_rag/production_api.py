"""Production FastAPI composition root for the remote Provider topology."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import cast

from fastapi import FastAPI
from starlette.applications import Starlette

from enterprise_rag.adapters.cleaners import DeterministicCleaner
from enterprise_rag.adapters.database import Database, PostgreSQLMcpTokenStore
from enterprise_rag.adapters.embeddings import OpenAICompatibleEmbedding
from enterprise_rag.adapters.llms import OpenAICompatibleLanguageModel
from enterprise_rag.adapters.object_store import LocalObjectStore
from enterprise_rag.adapters.planners import LanguageModelQueryPlanner
from enterprise_rag.adapters.rerankers import OpenAICompatibleReranker
from enterprise_rag.adapters.sparse import MilvusBuiltinBm25Encoder
from enterprise_rag.adapters.splitters import StructureAwareSplitter
from enterprise_rag.adapters.vector_store import MilvusRemoteVectorStore
from enterprise_rag.api import create_app
from enterprise_rag.config import AppSettings, load_settings
from enterprise_rag.mcp import build_http_mcp_app
from enterprise_rag.mcp.catalog import McpCapabilityCatalog
from enterprise_rag.mcp.knowledge_catalog import McpKnowledgeCatalog
from enterprise_rag.mcp.local_credentials import resolve_mcp_token_pepper
from enterprise_rag.ports import Provider, ProviderRegistry
from enterprise_rag.services import (
    BoundedLanguageModel,
    DeterministicEvaluator,
    DualSearchService,
    KnowledgeApplication,
    ManualLlmCleaningService,
    McpApplicationService,
    ProjectionService,
    ProviderReindexService,
    QueryPlanningService,
    ReciprocalRankFusion,
    SemanticQueryRunner,
    build_persistent_tracing,
)
from enterprise_rag.services.index_revision import index_revision
from enterprise_rag.services.provider_catalog import (
    SILICONFLOW_EMBEDDING_MODEL,
    RuntimeProviderCatalog,
)
from enterprise_rag.services.vision_provider import build_vision_provider
from enterprise_rag.services.workspace import WorkspaceService

LOGGER = logging.getLogger(__name__)
Closer = Callable[[], Awaitable[None]]


def build_production_api_app(settings: AppSettings | None = None) -> FastAPI:
    """Compose the production API against shared remote infrastructure.

    The API owns query and administrative resources only. Ingestion jobs are
    claimed by the separate ``enterprise-rag-worker`` process, so this root
    deliberately provides no embedded background Worker and never opens a
    Milvus Lite file.
    """

    active = settings or load_settings()
    _validate_provider_topology(active)
    credentials = active.credentials
    assert credentials.database_url is not None
    assert credentials.session_secret is not None
    assert credentials.llm_base_url is not None
    assert credentials.llm_api_key is not None
    assert credentials.llm_model is not None
    assert credentials.embedding_base_url is not None
    assert credentials.embedding_api_key is not None
    assert credentials.embedding_model is not None
    assert credentials.rerank_base_url is not None
    assert credentials.rerank_api_key is not None
    assert credentials.rerank_model is not None
    assert credentials.vector_store_uri is not None
    assert credentials.vector_store_token is not None
    assert credentials.mcp_token_pepper is not None
    assert active.app.mcp_public_base_url is not None

    runtime_root = active.ingestion.object_store_root.resolve()
    selection_path = runtime_root.parent / "provider-selection.json"
    # Production Provider identity is configuration-bound. The restart-bound
    # admin selection file is a local-demo feature and must not silently turn
    # a remote production adapter into an arbitrary model request.
    embedding_model = credentials.embedding_model
    reranker_model = credentials.rerank_model
    llm_model = credentials.llm_model

    acquired: list[Closer] = []
    try:
        database = Database(credentials.database_url.get_secret_value())
        acquired.append(database.dispose)
        object_store = LocalObjectStore(runtime_root)
        acquired.append(object_store.aclose)

        embedding = OpenAICompatibleEmbedding(
            base_url=str(credentials.embedding_base_url),
            api_key=credentials.embedding_api_key.get_secret_value(),
            model=embedding_model,
            provider_name="openai_compatible",
            dimension=active.ingestion.embedding_dimension,
            input_token_limit=(8192 if embedding_model == SILICONFLOW_EMBEDDING_MODEL else None),
            tokenizer_name=f"estimated-tokenizer:{embedding_model}",
            batch_size=active.ingestion.embedding_batch_size,
            max_batch_tokens=active.ingestion.embedding_batch_tokens,
            timeout_seconds=active.ingestion.embedding_timeout_seconds,
            max_retries=active.ingestion.embedding_max_retries,
        )
        acquired.append(embedding.aclose)
        sparse = MilvusBuiltinBm25Encoder()
        acquired.append(sparse.aclose)
        vector_store = MilvusRemoteVectorStore(
            str(credentials.vector_store_uri),
            token=credentials.vector_store_token.get_secret_value(),
            db_name=credentials.vector_store_database,
        )
        acquired.append(vector_store.aclose)
        reranker = OpenAICompatibleReranker(
            base_url=str(credentials.rerank_base_url),
            api_key=credentials.rerank_api_key.get_secret_value(),
            model=reranker_model,
            provider_name="openai_compatible",
            timeout_seconds=active.cost_guard.provider_timeout_seconds,
            max_retries=active.cost_guard.provider_max_retries,
        )
        acquired.append(reranker.aclose)
        raw_language_model = OpenAICompatibleLanguageModel(
            base_url=str(credentials.llm_base_url),
            api_key=credentials.llm_api_key.get_secret_value(),
            model=llm_model,
            timeout_seconds=active.cost_guard.provider_timeout_seconds,
        )
        acquired.append(raw_language_model.aclose)
        language_model = BoundedLanguageModel(
            raw_language_model,
            timeout_seconds=active.cost_guard.provider_timeout_seconds,
            max_retries=active.cost_guard.provider_max_retries,
            retry_backoff_seconds=active.cost_guard.provider_retry_backoff_seconds,
        )
        splitter = StructureAwareSplitter(
            target_tokens=active.ingestion.target_tokens,
            max_tokens=active.ingestion.max_tokens,
            overlap_tokens=active.ingestion.overlap_tokens,
            token_counter=embedding.count_tokens,
            tokenizer=embedding.tokenizer_name,
            embedding_token_limit=embedding.input_token_limit,
        )
        acquired.append(splitter.aclose)
        cleaner = DeterministicCleaner()
        acquired.append(cleaner.aclose)
        vision = build_vision_provider(active)
        acquired.append(vision.aclose)
        evaluator = DeterministicEvaluator()
        acquired.append(evaluator.aclose)

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

        acquired.append(close_tracer)
        query_runner = SemanticQueryRunner(
            database=database,
            embedding=embedding,
            sparse=sparse,
            vector_store=vector_store,
            reranker=reranker,
            language_model=language_model,
            index_revision=revision,
            dense_top_k=active.retrieval.dense_candidates,
            sparse_top_k=active.retrieval.sparse_candidates,
            fused_top_k=active.retrieval.fused_candidates,
            rerank_candidates=active.retrieval.rerank_candidates,
            selected_leaf_k=active.retrieval.selected_leaf_k,
            rrf_k=active.retrieval.rrf_k,
            max_parent_chars=active.retrieval.max_parent_chars,
            max_output_tokens=active.cost_guard.answer_max_output_tokens,
            # The LLM Planner is intentionally part of the same bounded
            # LanguageModel lifecycle as answer generation and cleaning.
            planner=QueryPlanningService(LanguageModelQueryPlanner(language_model)),
        )
        registry = ProviderRegistry()
        for provider in (
            embedding,
            sparse,
            vector_store,
            reranker,
            language_model,
            splitter,
            cleaner,
            vision,
            evaluator,
        ):
            registry.register(cast(Provider, provider))

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
                "vision": vision.info().name,
                "sparse_encoder": sparse_info.name,
            },
            current_embedding_dimension=embedding.dimension,
            current_embedding_input_token_limit=embedding.input_token_limit,
            remote_credentials=frozenset(
                {"embedding", "reranker"}
                | (
                    {"vision"}
                    if active.providers.vision == "openai_compatible"
                    else set()
                )
            ),
        )
        provider_reindex = ProviderReindexService(
            database=database,
            splitter=splitter,
            projection=projection,
            vector_store=vector_store,
            active_revision=revision,
            embedding_model=embedding_model,
            embedding_dimension=embedding.dimension,
            temporary_root=runtime_root.parent / "provider-reindex-temporary",
            max_documents=active.security.anonymous_max_ready_documents,
        )
        mcp_base_url = str(active.app.mcp_public_base_url).rstrip("/")
        mcp_pepper = resolve_mcp_token_pepper(
            credentials.mcp_token_pepper.get_secret_value(),
            path=runtime_root.parent / "mcp-token-pepper",
            production=True,
        )
        workspace = WorkspaceService(
            database,
            object_store,
            max_upload_bytes=min(
                active.ingestion.max_upload_bytes,
                active.security.anonymous_max_file_bytes,
            ),
            max_documents=active.security.anonymous_max_ready_documents,
            max_attempts=active.ingestion.max_attempts,
        )
        mcp_knowledge_catalog = McpKnowledgeCatalog(
            database=database,
            workspace=workspace,
            search=DualSearchService(
                embedding=embedding,
                sparse=sparse,
                vector_store=vector_store,
                dense_top_k=active.retrieval.dense_candidates,
                sparse_top_k=active.retrieval.sparse_candidates,
            ),
            fusion=ReciprocalRankFusion(
                rrf_k=active.retrieval.rrf_k,
                top_k=min(active.retrieval.fused_candidates, 20),
                max_leaves_per_root=3,
            ),
            index_revision=revision,
        )

        def mcp_http_factory(knowledge: KnowledgeApplication) -> Starlette:
            return build_http_mcp_app(
                McpApplicationService(knowledge),
                mcp_knowledge_catalog,
                PostgreSQLMcpTokenStore(database),
                token_pepper=mcp_pepper,
                public_base_url=mcp_base_url,
                allow_insecure_http=False,
            )

        capabilities = McpCapabilityCatalog.from_environment()
        mcp_capabilities = McpCapabilityCatalog(
            capabilities.stdio_factory_declared,
            http_mounted_endpoint=f"{mcp_base_url}/mcp",
        )
        application = create_app(
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
            mcp_catalog=mcp_capabilities,
            mcp_http_factory=mcp_http_factory,
            resource_closers=(database.dispose, object_store.aclose, registry.aclose, close_tracer),
        )
        application.state.production_provider_registry = registry
        application.state.production_index_revision = revision
        application.state.production_background_tasks = ()
        acquired.clear()
        return application
    except BaseException:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_close_resources(acquired))
        else:
            loop.create_task(_close_resources(acquired))
        raise


def _validate_provider_topology(settings: AppSettings) -> None:
    if settings.app.environment != "production":
        raise RuntimeError("The production API requires app.environment=production")
    expected = {
        "llm": "openai_compatible",
        "embedding": "openai_compatible",
        "reranker": "openai_compatible",
        "vector_store": "milvus_remote",
        "sparse_encoder": "milvus_builtin_bm25",
    }
    mismatched = [
        f"{name}={getattr(settings.providers, name)} (expected {value})"
        for name, value in expected.items()
        if getattr(settings.providers, name) != value
    ]
    if mismatched:
        raise RuntimeError("Production Provider topology is invalid: " + ", ".join(mismatched))
    credentials = settings.credentials
    required = (
        credentials.database_url,
        credentials.session_secret,
        credentials.llm_base_url,
        credentials.llm_api_key,
        credentials.llm_model,
        credentials.embedding_base_url,
        credentials.embedding_api_key,
        credentials.embedding_model,
        credentials.rerank_base_url,
        credentials.rerank_api_key,
        credentials.rerank_model,
        credentials.vector_store_uri,
        credentials.vector_store_token,
        credentials.mcp_token_pepper,
    )
    if any(value is None or (isinstance(value, str) and not value.strip()) for value in required):
        raise RuntimeError("Production API credentials are incomplete")
    if settings.app.mcp_public_base_url is None:
        raise RuntimeError("MCP_PUBLIC_BASE_URL is required by the production API")
    if str(settings.app.mcp_public_base_url).casefold().startswith("http://"):
        raise RuntimeError("MCP_PUBLIC_BASE_URL must use HTTPS in production")


async def _close_resources(closers: list[Closer]) -> None:
    for closer in reversed(closers):
        try:
            await closer()
        except Exception:
            LOGGER.warning(
                "rag.production_api.resource_close_failed",
                extra={"event_code": "PRODUCTION_API_RESOURCE_CLOSE_FAILED", "outcome": "degraded"},
            )


def main() -> int:
    """Keep a dedicated production console entrypoint for operational clarity."""

    settings = load_settings()
    from enterprise_rag.observability import configure_json_logging

    configure_json_logging(environment=settings.app.environment)
    import uvicorn

    uvicorn.run(
        build_production_api_app(settings),
        host="0.0.0.0",
        port=8000,
        workers=1,
    )
    return 0


__all__ = ["build_production_api_app", "main"]
