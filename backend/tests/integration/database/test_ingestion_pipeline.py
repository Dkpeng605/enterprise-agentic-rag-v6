import os
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text

from enterprise_rag.adapters.cleaners import DeterministicCleaner
from enterprise_rag.adapters.database import Database, IngestionContentRepository
from enterprise_rag.adapters.database.jobs import IngestionJobRepository
from enterprise_rag.adapters.database.models import (
    CollectionModel,
    DocumentModel,
    DocumentVersionModel,
    IngestionJobModel,
    LeafModel,
    RootModel,
    TenantModel,
    UserModel,
)
from enterprise_rag.adapters.embeddings import HashingDenseEmbedding
from enterprise_rag.adapters.loaders import TextDocumentLoader
from enterprise_rag.adapters.object_store import LocalObjectStore
from enterprise_rag.adapters.sparse import HashingSparseEncoder
from enterprise_rag.adapters.splitters import StructureAwareSplitter
from enterprise_rag.adapters.vector_store import MilvusLiteVectorStore
from enterprise_rag.adapters.vision import NoopVisionProvider
from enterprise_rag.domain import AppError, ErrorCode, JobStatus, QueryMode, QueryScope
from enterprise_rag.domain.common import new_uuid7
from enterprise_rag.ports import (
    CompletionRequest,
    CompletionResult,
    ProviderHealth,
    ProviderInfo,
    ProviderKind,
    RerankCandidate,
    RerankResult,
)
from enterprise_rag.services import (
    DeterministicLocalQueryRunner,
    DocumentRegistrationService,
    ImageEnricher,
    IngestionPipeline,
    ProjectionService,
    RegisterDocument,
    SemanticQueryRunner,
    WorkspaceService,
)
from enterprise_rag.services.query_api import QueryCommand, QueryProgress

BACKEND_ROOT = Path(__file__).parents[3]
DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test",
)
TENANT_ID = UUID("01900000-0000-7000-8000-000000001201")
USER_ID = UUID("01900000-0000-7000-8000-000000001202")
COLLECTION_ID = UUID("01900000-0000-7000-8000-000000001203")
NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


class FakeEmbedding:
    dimension = 3

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            ProviderKind.EMBEDDING,
            "fake",
            "1",
            frozenset({"documents", "query"}),
            False,
            ProviderHealth.HEALTHY,
        )

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, float(index + 1), 0.5] for index, _ in enumerate(texts)]

    async def embed_query(self, text: str) -> list[float]:
        del text
        return [1.0, 1.0, 0.5]

    async def aclose(self) -> None:
        return None


class FakeReranker:
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            ProviderKind.RERANKER,
            "fake_cross_encoder",
            "1",
            frozenset({"cross-encoder"}),
            False,
            ProviderHealth.HEALTHY,
        )

    async def rerank(
        self,
        query: str,
        candidates: Sequence[RerankCandidate],
        *,
        top_k: int,
    ) -> list[RerankResult]:
        assert query
        return [
            RerankResult(candidate.candidate_id, float(len(candidates) - index))
            for index, candidate in enumerate(candidates[:top_k])
        ]

    async def aclose(self) -> None:
        return None


class FakeLanguageModel:
    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            ProviderKind.LLM,
            "fake_openai_compatible",
            "minimax-test",
            frozenset({"chat-completions"}),
            True,
            ProviderHealth.HEALTHY,
        )

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        return CompletionResult("企业知识库策略来自已上传文档 [1]。", 81, 17)

    async def aclose(self) -> None:
        return None


@pytest.fixture(scope="module", autouse=True)
def ensure_schema() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")


async def payload(value: bytes) -> AsyncIterator[bytes]:
    yield value[:5]
    yield value[5:]


async def seed(database: Database) -> None:
    async with database.session() as session:
        await session.execute(text("TRUNCATE TABLE users, tenants CASCADE"))
        session.add_all(
            [
                TenantModel(id=TENANT_ID, name="Pipeline", slug="pipeline"),
                UserModel(
                    id=USER_ID,
                    email="pipeline@example.invalid",
                    password_hash="not-a-real-hash",
                ),
            ]
        )
        await session.flush()
        session.add(CollectionModel(id=COLLECTION_ID, tenant_id=TENANT_ID, name="Pipeline"))


async def submit(
    database: Database, store: LocalObjectStore, *, max_attempts: int = 1
) -> tuple[UUID, UUID, UUID]:
    registration = await DocumentRegistrationService(
        database, store, max_attempts=max_attempts
    ).register(
        RegisterDocument(
            tenant_id=TENANT_ID,
            collection_id=COLLECTION_ID,
            created_by=USER_ID,
            logical_name="policy",
            title="Policy",
            source_name="policy.txt",
            media_type="text/plain",
        ),
        payload("企业知识库  Enterprise RAG policy evidence. ".encode() * 12),
        now=NOW,
    )
    assert registration.job_id is not None
    return registration.document_id, registration.version_id, registration.job_id


def build_pipeline(
    database: Database,
    store: LocalObjectStore,
    vector_store: MilvusLiteVectorStore,
    temporary_root: Path,
) -> tuple[
    IngestionPipeline,
    TextDocumentLoader,
    ImageEnricher,
    DeterministicCleaner,
    StructureAwareSplitter,
    ProjectionService,
]:
    loader = TextDocumentLoader()
    images = ImageEnricher(store, NoopVisionProvider())
    cleaner = DeterministicCleaner()
    splitter = StructureAwareSplitter(target_tokens=20, max_tokens=28, overlap_tokens=4)
    projection = ProjectionService(
        embedding=FakeEmbedding(),
        sparse=HashingSparseEncoder(),
        vector_store=vector_store,
        batch_size=2,
    )
    pipeline = IngestionPipeline(
        database=database,
        object_store=store,
        loaders=(loader,),
        cleaner=cleaner,
        splitter=splitter,
        image_enricher=images,
        projection=projection,
        vector_store=vector_store,
        temporary_root=temporary_root,
        index_revision="pipeline-v1",
        lease_for=timedelta(minutes=5),
        retry_delay=timedelta(0),
        clock=lambda: NOW,
    )
    return pipeline, loader, images, cleaner, splitter, projection


@pytest.mark.anyio
async def test_pipeline_runs_registered_object_to_ready_postgres_and_milvus(
    tmp_path: Path,
) -> None:
    database = Database(DATABASE_URL)
    store = LocalObjectStore(tmp_path / "objects")
    vector_store = MilvusLiteVectorStore(tmp_path / "vectors.db")
    try:
        await seed(database)
        document_id, version_id, job_id = await submit(database, store)
        pipeline, *_ = build_pipeline(database, store, vector_store, tmp_path / "temporary")

        result = await pipeline.run_once(owner="worker-a")

        assert result is not None and result.completed
        assert result.job.id == job_id and result.job.status is JobStatus.SUCCEEDED
        assert result.job.progress == 100
        async with database.session() as session:
            document = await session.get(DocumentModel, document_id)
            version = await session.get(DocumentVersionModel, version_id)
            root_count = await session.scalar(
                select(func.count())
                .select_from(RootModel)
                .where(RootModel.version_id == version_id)
            )
            leaf_count = await session.scalar(
                select(func.count())
                .select_from(LeafModel)
                .where(LeafModel.version_id == version_id)
            )
        assert document is not None and document.status == "ready"
        assert document.active_version_id == version_id
        assert version is not None and version.status == "indexed"
        assert root_count == 1 and leaf_count is not None and leaf_count > 1
        assert await vector_store.count_by_version(TENANT_ID, version_id) == leaf_count

        workspace = WorkspaceService(
            database,
            store,
            max_upload_bytes=1024 * 1024,
            max_documents=100,
            max_attempts=3,
        )
        inspection = await workspace.inspect_document_pipeline(
            TENANT_ID, document_id, cursor=None, limit=50
        )
        assert inspection.root_count == 1
        assert inspection.leaf_count == leaf_count
        assert inspection.parser_provider == "text_documents"
        assert inspection.parser_version == "1"
        assert inspection.cleaner_provider == "deterministic"
        assert inspection.splitter_provider == "structure_aware"
        assert inspection.splitter_settings == {
            "configured_target_tokens": 20,
            "configured_max_tokens": 28,
            "configured_overlap_tokens": 4,
            "target_tokens": 20,
            "max_tokens": 28,
            "overlap_tokens": 4,
            "embedding_token_limit": None,
            "embedding_safety_margin": 1,
            "tokenizer": "deterministic-multilingual-v1",
            "boundary_policy": (
                "code_block>table_row>heading/list>paragraph>sentence>token_hard_cut"
            ),
            "hard_cut_count": 0,
        }
        assert inspection.roots[0].changed
        assert inspection.roots[0].cleaning_audit[0].rule == "whitespace"

        detail = await workspace.inspect_pipeline_root(
            TENANT_ID, document_id, inspection.roots[0].id
        )
        assert "知识库  Enterprise" in detail.raw_text
        assert "知识库  Enterprise" not in detail.clean_text
        assert len(detail.leaves) == leaf_count
        assert detail.leaves[0].start_offset == 0
        assert detail.leaves[0].token_count > 0
        assert detail.leaves[0].metadata["token_budget"] == 28
        assert detail.leaves[0].metadata["hard_cut"] is False
        assert any(leaf.overlap_chars > 0 for leaf in detail.leaves[1:])
        with pytest.raises(AppError) as hidden_document:
            await workspace.inspect_document_pipeline(
                UUID("01900000-0000-7000-8000-000000009999"),
                document_id,
                cursor=None,
                limit=50,
            )
        assert hidden_document.value.code is ErrorCode.NOT_FOUND
        with pytest.raises(AppError) as hidden_root:
            await workspace.inspect_pipeline_root(TENANT_ID, document_id, "root_unknown")
        assert hidden_root.value.code is ErrorCode.NOT_FOUND
        assert await pipeline.run_once(owner="worker-a") is None
        assert list((tmp_path / "temporary").iterdir()) == []
    finally:
        await vector_store.aclose()
        await database.dispose()


@pytest.mark.anyio
async def test_pipeline_output_is_queryable_with_citations_and_ordered_progress(
    tmp_path: Path,
) -> None:
    database = Database(DATABASE_URL)
    store = LocalObjectStore(tmp_path / "objects")
    vector_store = MilvusLiteVectorStore(tmp_path / "vectors.db")
    embedding = HashingDenseEmbedding(dimension=64)
    sparse = HashingSparseEncoder()
    try:
        await seed(database)
        document_id, _, _ = await submit(database, store)
        pipeline = IngestionPipeline(
            database=database,
            object_store=store,
            loaders=(TextDocumentLoader(),),
            cleaner=DeterministicCleaner(),
            splitter=StructureAwareSplitter(target_tokens=20, max_tokens=28, overlap_tokens=4),
            image_enricher=ImageEnricher(store, NoopVisionProvider()),
            projection=ProjectionService(
                embedding=embedding,
                sparse=sparse,
                vector_store=vector_store,
                batch_size=2,
            ),
            vector_store=vector_store,
            temporary_root=tmp_path / "temporary",
            index_revision="local-query-v1",
            retry_delay=timedelta(0),
            clock=lambda: NOW,
        )
        completed = await pipeline.run_once(owner="query-worker")
        assert completed is not None and completed.completed

        runner = DeterministicLocalQueryRunner(
            database=database,
            embedding=embedding,
            sparse=sparse,
            vector_store=vector_store,
            index_revision="local-query-v1",
            dense_top_k=10,
            sparse_top_k=10,
            fused_top_k=10,
            rerank_candidates=10,
            selected_leaf_k=3,
        )
        progress: list[QueryProgress] = []

        async def emit(item: QueryProgress) -> None:
            progress.append(item)

        result = await runner.run(
            QueryCommand(
                new_uuid7(),
                TENANT_ID,
                USER_ID,
                "Enterprise RAG policy evidence",
                QueryMode.STANDARD,
                QueryScope(document_ids=(document_id,)),
                (),
            ),
            emit=emit,
        )

        assert result.status.value == "answered"
        assert "Enterprise RAG policy evidence" in result.answer
        assert result.citations and result.citations[0].document_id == document_id
        assert result.usage == {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0}
        assert [item.stage.value for item in progress] == [
            "planning",
            "retrieving",
            "reranking",
            "recovering",
            "answering",
        ]

        language_model = FakeLanguageModel()
        semantic_runner = SemanticQueryRunner(
            database=database,
            embedding=embedding,
            sparse=sparse,
            vector_store=vector_store,
            reranker=FakeReranker(),
            language_model=language_model,
            index_revision="local-query-v1",
            dense_top_k=10,
            sparse_top_k=10,
            fused_top_k=10,
            rerank_candidates=10,
            selected_leaf_k=3,
        )
        semantic_progress: list[QueryProgress] = []

        async def emit_semantic(item: QueryProgress) -> None:
            semantic_progress.append(item)

        semantic = await semantic_runner.run(
            QueryCommand(
                new_uuid7(),
                TENANT_ID,
                USER_ID,
                "企业知识库策略是什么？",
                QueryMode.DEEP,
                QueryScope(document_ids=(document_id,)),
                (),
            ),
            emit=emit_semantic,
        )

        assert semantic.status.value == "answered"
        assert semantic.answer.endswith("[1]。")
        assert semantic.citations[0].document_id == document_id
        assert semantic.usage == {"llm_calls": 1, "input_tokens": 81, "output_tokens": 17}
        assert semantic.diagnostics["mode"] == "deep"
        assert semantic.diagnostics["reranker_provider"] == "fake_cross_encoder"
        assert language_model.requests
        assert "[证据 1]" in language_model.requests[0].user_prompt
        assert [item.stage.value for item in semantic_progress] == [
            "planning",
            "retrieving",
            "reranking",
            "recovering",
            "answering",
        ]
    finally:
        await vector_store.aclose()
        await database.dispose()


@pytest.mark.parametrize(
    "stage",
    ["loading", "images", "cleaning", "splitting", "persisting", "projecting", "finalizing"],
)
@pytest.mark.anyio
async def test_every_pipeline_stage_failure_is_compensated_and_sanitized(
    stage: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(DATABASE_URL)
    store = LocalObjectStore(tmp_path / "objects")
    vector_store = MilvusLiteVectorStore(tmp_path / "vectors.db")
    try:
        await seed(database)
        document_id, version_id, _ = await submit(database, store)
        pipeline, loader, images, cleaner, splitter, projection = build_pipeline(
            database, store, vector_store, tmp_path / "temporary"
        )

        async def fail(*args: object, **kwargs: object) -> Any:
            del args, kwargs
            raise RuntimeError("sensitive injected stage failure")

        targets: dict[str, tuple[object, str]] = {
            "loading": (loader, "load"),
            "images": (images, "enrich"),
            "cleaning": (cleaner, "clean_all"),
            "splitting": (splitter, "split"),
            "persisting": (IngestionContentRepository, "replace_content"),
            "projecting": (projection, "project"),
            "finalizing": (IngestionContentRepository, "finalize"),
        }
        target, method = targets[stage]
        monkeypatch.setattr(target, method, fail)

        result = await pipeline.run_once(owner=f"worker-{stage}")

        assert result is not None and not result.completed
        assert result.job.status is JobStatus.FAILED
        assert result.job.error_code == "INTERNAL_ERROR"
        assert result.job.error_message == "The ingestion Pipeline failed."
        assert "sensitive" not in result.job.error_message
        assert await vector_store.count_by_version(TENANT_ID, version_id) == 0
        async with database.session() as session:
            document = await session.get(DocumentModel, document_id)
            version = await session.get(DocumentVersionModel, version_id)
            content_count = await session.scalar(
                select(func.count())
                .select_from(RootModel)
                .where(RootModel.version_id == version_id)
            )
        assert document is not None and document.status == "failed"
        assert version is not None and version.status == "failed"
        assert content_count == 0
    finally:
        await vector_store.aclose()
        await database.dispose()


@pytest.mark.anyio
async def test_running_job_cancellation_is_acknowledged_and_compensated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(DATABASE_URL)
    store = LocalObjectStore(tmp_path / "objects")
    vector_store = MilvusLiteVectorStore(tmp_path / "vectors.db")
    try:
        await seed(database)
        _, version_id, job_id = await submit(database, store)
        pipeline, loader, *_ = build_pipeline(database, store, vector_store, tmp_path / "temporary")
        original_load = loader.load

        async def load_and_cancel(*args: Any, **kwargs: Any) -> Any:
            roots = await original_load(*args, **kwargs)
            async with database.session() as session:
                await IngestionJobRepository(session).request_cancel(job_id)
            return roots

        monkeypatch.setattr(loader, "load", load_and_cancel)
        result = await pipeline.run_once(owner="worker-cancel")

        assert result is not None and result.job.status is JobStatus.CANCELLED
        assert not result.completed
        assert await vector_store.count_by_version(TENANT_ID, version_id) == 0
        async with database.session() as session:
            job = await session.get(IngestionJobModel, job_id)
            version = await session.get(DocumentVersionModel, version_id)
        assert job is not None and job.stage == "cancelled"
        assert version is not None and version.error_code == "JOB_CANCELLED"
    finally:
        await vector_store.aclose()
        await database.dispose()


@pytest.mark.anyio
async def test_transient_failure_retries_from_clean_state_and_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(DATABASE_URL)
    store = LocalObjectStore(tmp_path / "objects")
    vector_store = MilvusLiteVectorStore(tmp_path / "vectors.db")
    try:
        await seed(database)
        document_id, version_id, job_id = await submit(database, store, max_attempts=2)
        pipeline, loader, *_ = build_pipeline(database, store, vector_store, tmp_path / "temporary")
        original_load = loader.load
        calls = 0

        async def fail_once(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("temporary private failure")
            return await original_load(*args, **kwargs)

        monkeypatch.setattr(loader, "load", fail_once)

        first = await pipeline.run_once(owner="worker-retry")
        assert first is not None and first.job.status is JobStatus.RETRY_WAIT
        assert first.job.attempts == 1
        assert first.job.error_code == ErrorCode.INTERNAL_ERROR.value
        assert await vector_store.count_by_version(TENANT_ID, version_id) == 0

        second = await pipeline.run_once(owner="worker-retry")
        assert second is not None and second.completed
        assert second.job.id == job_id
        assert second.job.status is JobStatus.SUCCEEDED
        assert second.job.attempts == 2
        async with database.session() as session:
            document = await session.get(DocumentModel, document_id)
            version = await session.get(DocumentVersionModel, version_id)
        assert document is not None and document.status == "ready"
        assert version is not None and version.status == "indexed"
    finally:
        await vector_store.aclose()
        await database.dispose()
