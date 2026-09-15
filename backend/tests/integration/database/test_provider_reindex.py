import os
from collections.abc import Sequence
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from enterprise_rag.adapters.database import Database
from enterprise_rag.adapters.database.models import (
    CollectionModel,
    DocumentModel,
    DocumentVersionModel,
    LeafModel,
    RootModel,
    TenantModel,
    UserModel,
)
from enterprise_rag.adapters.sparse import HashingSparseEncoder
from enterprise_rag.adapters.splitters import StructureAwareSplitter
from enterprise_rag.adapters.vector_store import MilvusLiteVectorStore
from enterprise_rag.domain import LeafChunk, RootChunk, RootKind
from enterprise_rag.ports import (
    DenseSearchRequest,
    IndexSchema,
    ProviderHealth,
    ProviderInfo,
    ProviderKind,
    VectorRecord,
)
from enterprise_rag.services import ProjectionRequest, ProjectionService, ProviderReindexService

BACKEND_ROOT = Path(__file__).parents[3]
DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test",
)
TENANT_ID = UUID("01900000-0000-7000-8000-000000002101")
USER_ID = UUID("01900000-0000-7000-8000-000000002102")
COLLECTION_ID = UUID("01900000-0000-7000-8000-000000002103")
DOCUMENT_ID = UUID("01900000-0000-7000-8000-000000002104")
VERSION_ID = UUID("01900000-0000-7000-8000-000000002105")
OLD_REVISION = "provider-old-v1"
NEW_REVISION = "provider-new-v2"


@pytest.fixture(scope="module", autouse=True)
def ensure_schema() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")


class FakeEmbedding:
    dimension = 3

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            ProviderKind.EMBEDDING,
            "provider-reindex-test",
            "1",
            frozenset({"documents", "query"}),
            False,
            ProviderHealth.HEALTHY,
        )

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [[1.0, float(index), 0.1] for index, _ in enumerate(texts)]

    async def embed_query(self, text: str) -> list[float]:
        del text
        return [1.0, 0.0, 0.1]

    async def aclose(self) -> None:
        return None


class PartiallyFailingProjection:
    def __init__(self, vector_store: MilvusLiteVectorStore) -> None:
        self._vector_store = vector_store

    async def project(self, request: ProjectionRequest) -> None:
        await self._vector_store.ensure_revision(IndexSchema(request.index_revision, 3))
        leaf = request.leaves[0]
        await self._vector_store.upsert(
            [
                VectorRecord(
                    index_revision=request.index_revision,
                    leaf_id=leaf.id,
                    root_id=leaf.root_id,
                    tenant_id=request.tenant_id,
                    collection_id=request.collection_id,
                    document_id=request.document_id,
                    version_id=request.version_id,
                    status="processing",
                    dense_vector=(1.0, 0.0, 0.1),
                    sparse_vector={1: 1.0},
                    metadata={},
                )
            ]
        )
        raise RuntimeError("injected projection failure")


class SilentlyIncompleteProjection:
    """A broken projection adapter that reports success without writing vectors."""

    async def project(self, request: ProjectionRequest) -> None:
        del request
        return None


def chunks(revision: str) -> tuple[RootChunk, LeafChunk]:
    root = RootChunk.create(
        tenant_id=TENANT_ID,
        document_id=DOCUMENT_ID,
        version_id=VERSION_ID,
        index_revision=revision,
        ordinal=0,
        kind=RootKind.SECTION,
        source_locator={"section": "provider-reindex"},
        raw_text="Provider index migration keeps the active document searchable.",
        clean_text="Provider index migration keeps the active document searchable.",
        metadata={"fixture": "provider-reindex"},
    )
    leaf = LeafChunk.create(
        root_id=root.id,
        tenant_id=TENANT_ID,
        document_id=DOCUMENT_ID,
        version_id=VERSION_ID,
        ordinal=0,
        text=root.clean_text,
        retrieval_text=root.clean_text,
        start_offset=0,
        end_offset=len(root.clean_text),
        token_count=8,
        metadata={"fixture": "provider-reindex"},
    )
    return root, leaf


async def seed_database(database: Database, revision: str) -> LeafChunk:
    root, leaf = chunks(revision)
    async with database.session() as session:
        await session.execute(text("TRUNCATE TABLE users, tenants CASCADE"))
        session.add_all(
            [
                TenantModel(id=TENANT_ID, name="Provider Reindex", slug="provider-reindex"),
                UserModel(
                    id=USER_ID,
                    email="provider-reindex@example.invalid",
                    password_hash="not-a-password",
                ),
            ]
        )
        await session.flush()
        session.add(
            CollectionModel(
                id=COLLECTION_ID,
                tenant_id=TENANT_ID,
                name="Provider Reindex",
            )
        )
        await session.flush()
        document = DocumentModel(
            id=DOCUMENT_ID,
            tenant_id=TENANT_ID,
            collection_id=COLLECTION_ID,
            logical_name="provider-reindex",
            title="Provider reindex fixture",
            status="ready",
            created_by=USER_ID,
        )
        session.add(document)
        await session.flush()
        session.add(
            DocumentVersionModel(
                id=VERSION_ID,
                document_id=DOCUMENT_ID,
                sha256="a" * 64,
                source_name="provider-reindex.txt",
                media_type="text/plain",
                size_bytes=len(root.raw_text),
                object_key=f"sha256/aa/aa/{'a' * 64}",
                parser_provider="test",
                parser_version="1",
                status="indexed",
            )
        )
        await session.flush()
        document.active_version_id = VERSION_ID
        session.add(
            RootModel(
                id=root.id,
                tenant_id=root.tenant_id,
                document_id=root.document_id,
                version_id=root.version_id,
                index_revision=root.index_revision,
                ordinal=root.ordinal,
                kind=root.kind.value,
                source_locator=dict(root.source_locator),
                raw_text=root.raw_text,
                clean_text=root.clean_text,
                metadata_json=dict(root.metadata),
                content_hash=root.content_hash,
            )
        )
        await session.flush()
        session.add(
            LeafModel(
                id=leaf.id,
                root_id=leaf.root_id,
                tenant_id=leaf.tenant_id,
                document_id=leaf.document_id,
                version_id=leaf.version_id,
                ordinal=leaf.ordinal,
                text=leaf.text,
                retrieval_text=leaf.retrieval_text,
                start_offset=leaf.start_offset,
                end_offset=leaf.end_offset,
                token_count=leaf.token_count,
                metadata_json=dict(leaf.metadata),
                content_hash=leaf.content_hash,
            )
        )
    return leaf


async def seed_vector(
    vector_store: MilvusLiteVectorStore, revision: str, leaf: LeafChunk
) -> None:
    await vector_store.ensure_revision(IndexSchema(revision, 3))
    await vector_store.upsert(
        [
            VectorRecord(
                index_revision=revision,
                leaf_id=leaf.id,
                root_id=leaf.root_id,
                tenant_id=TENANT_ID,
                collection_id=COLLECTION_ID,
                document_id=DOCUMENT_ID,
                version_id=VERSION_ID,
                status="ready",
                dense_vector=(1.0, 0.0, 0.1),
                sparse_vector={1: 1.0},
                metadata={},
            )
        ]
    )


def reindex_service(
    database: Database,
    vector_store: MilvusLiteVectorStore,
    projection: ProjectionService,
    temporary_root: Path,
) -> ProviderReindexService:
    return ProviderReindexService(
        database=database,
        splitter=StructureAwareSplitter(target_tokens=8, max_tokens=16, overlap_tokens=0),
        projection=projection,
        vector_store=vector_store,
        active_revision=NEW_REVISION,
        embedding_model="provider-new-model",
        embedding_dimension=3,
        temporary_root=temporary_root,
    )


def real_projection(vector_store: MilvusLiteVectorStore) -> ProjectionService:
    return ProjectionService(
        embedding=FakeEmbedding(),
        sparse=HashingSparseEncoder(),
        vector_store=vector_store,
    )


async def stored_revisions(database: Database) -> tuple[str, ...]:
    async with database.session() as session:
        values = await session.scalars(
            select(RootModel.index_revision).where(RootModel.version_id == VERSION_ID)
        )
        return tuple(values)


@pytest.mark.anyio
async def test_current_roots_with_missing_vectors_are_rebuilt_instead_of_skipped(
    tmp_path: Path,
) -> None:
    database = Database(DATABASE_URL)
    vector_store = MilvusLiteVectorStore(tmp_path / "missing-current-vectors.db")
    try:
        await seed_database(database, NEW_REVISION)
        service = reindex_service(
            database, vector_store, real_projection(vector_store), tmp_path / "temporary"
        )

        before = await service.status(TENANT_ID)
        result = await service.reindex(TENANT_ID)
        after = await service.status(TENANT_ID)

        assert before.incompatible_documents == 1
        assert result.rebuilt_count == 1
        assert result.skipped_count == 0
        assert after.compatible_documents == 1
        assert after.documents[0].vector_count == after.documents[0].leaf_count
    finally:
        await vector_store.aclose()
        await database.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("failure_stage", ["projection", "swap"])
async def test_failed_reindex_keeps_old_revision_queryable_and_removes_partial_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    database = Database(DATABASE_URL)
    vector_store = MilvusLiteVectorStore(tmp_path / f"failure-{failure_stage}.db")
    try:
        old_leaf = await seed_database(database, OLD_REVISION)
        await seed_vector(vector_store, OLD_REVISION, old_leaf)
        projection = (
            cast(ProjectionService, PartiallyFailingProjection(vector_store))
            if failure_stage == "projection"
            else real_projection(vector_store)
        )
        service = reindex_service(database, vector_store, projection, tmp_path / "temporary")
        if failure_stage == "swap":

            async def fail_swap(*args: object) -> None:
                del args
                raise RuntimeError("injected database swap failure")

            monkeypatch.setattr(service, "_swap_content", fail_swap)

        result = await service.reindex(TENANT_ID)
        old_hits = await vector_store.dense_search(
            DenseSearchRequest(OLD_REVISION, TENANT_ID, (1.0, 0.0, 0.1), 5)
        )

        assert result.failed_count == 1
        assert [hit.leaf_id for hit in old_hits] == [old_leaf.id]
        assert await vector_store.count_by_version_revision(
            TENANT_ID, VERSION_ID, NEW_REVISION
        ) == 0
        assert await stored_revisions(database) == (OLD_REVISION,)
    finally:
        await vector_store.aclose()
        await database.dispose()


@pytest.mark.anyio
async def test_successful_reindex_activates_new_revision_and_removes_all_old_vectors(
    tmp_path: Path,
) -> None:
    database = Database(DATABASE_URL)
    vector_store = MilvusLiteVectorStore(tmp_path / "successful-reindex.db")
    try:
        old_leaf = await seed_database(database, OLD_REVISION)
        await seed_vector(vector_store, OLD_REVISION, old_leaf)
        service = reindex_service(
            database, vector_store, real_projection(vector_store), tmp_path / "temporary"
        )

        result = await service.reindex(TENANT_ID)
        status = await service.status(TENANT_ID)
        old_hits = await vector_store.dense_search(
            DenseSearchRequest(OLD_REVISION, TENANT_ID, (1.0, 0.0, 0.1), 5)
        )
        new_hits = await vector_store.dense_search(
            DenseSearchRequest(NEW_REVISION, TENANT_ID, (1.0, 0.0, 0.1), 5)
        )

        assert result.rebuilt_count == 1
        assert status.compatible_documents == 1
        assert old_hits == []
        assert len(new_hits) == status.documents[0].leaf_count
        assert await vector_store.count_by_version_revision(
            TENANT_ID, VERSION_ID, OLD_REVISION
        ) == 0
        assert await stored_revisions(database) == (NEW_REVISION,)
    finally:
        await vector_store.aclose()
        await database.dispose()


@pytest.mark.anyio
async def test_silent_incomplete_projection_preserves_old_revision_and_database_facts(
    tmp_path: Path,
) -> None:
    database = Database(DATABASE_URL)
    vector_store = MilvusLiteVectorStore(tmp_path / "silent-incomplete-reindex.db")
    try:
        old_leaf = await seed_database(database, OLD_REVISION)
        await seed_vector(vector_store, OLD_REVISION, old_leaf)
        service = reindex_service(
            database,
            vector_store,
            cast(ProjectionService, SilentlyIncompleteProjection()),
            tmp_path / "temporary",
        )

        result = await service.reindex(TENANT_ID)
        old_hits = await vector_store.dense_search(
            DenseSearchRequest(OLD_REVISION, TENANT_ID, (1.0, 0.0, 0.1), 5)
        )

        assert result.failed_count == 1
        assert [hit.leaf_id for hit in old_hits] == [old_leaf.id]
        assert await vector_store.count_by_version_revision(
            TENANT_ID, VERSION_ID, NEW_REVISION
        ) == 0
        assert await stored_revisions(database) == (OLD_REVISION,)
    finally:
        await vector_store.aclose()
        await database.dispose()


@pytest.mark.anyio
async def test_incomplete_old_revision_cleanup_is_reported_without_hiding_old_vectors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(DATABASE_URL)
    vector_store = MilvusLiteVectorStore(tmp_path / "incomplete-cleanup-reindex.db")
    try:
        old_leaf = await seed_database(database, OLD_REVISION)
        await seed_vector(vector_store, OLD_REVISION, old_leaf)
        service = reindex_service(
            database, vector_store, real_projection(vector_store), tmp_path / "temporary"
        )

        async def silently_skip_delete(
            tenant_id: UUID, version_id: UUID, index_revision: str
        ) -> int:
            del tenant_id, version_id, index_revision
            return 0

        monkeypatch.setattr(vector_store, "delete_by_version_revision", silently_skip_delete)

        result = await service.reindex(TENANT_ID)

        assert result.rebuilt_count == 1
        assert result.cleanup_failed_count == 1
        assert result.items[0].status == "rebuilt_cleanup_degraded"
        assert await vector_store.count_by_version_revision(
            TENANT_ID, VERSION_ID, OLD_REVISION
        ) == 1
        assert await vector_store.count_by_version_revision(
            TENANT_ID, VERSION_ID, NEW_REVISION
        ) == 1
    finally:
        await vector_store.aclose()
        await database.dispose()
