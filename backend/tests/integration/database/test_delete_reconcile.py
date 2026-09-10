import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text

from enterprise_rag.adapters.database import Database, IngestionJobRepository
from enterprise_rag.adapters.database.models import (
    CollectionModel,
    DocumentContentClaimModel,
    DocumentModel,
    DocumentVersionModel,
    IngestionJobModel,
    LeafModel,
    RootModel,
    TenantModel,
    UserModel,
)
from enterprise_rag.adapters.object_store import LocalObjectStore
from enterprise_rag.adapters.vector_store import MilvusLiteVectorStore
from enterprise_rag.ports import IndexSchema, VectorRecord
from enterprise_rag.services import (
    DeletionStep,
    DocumentDeletionService,
    DocumentRegistrationService,
    ReconcileIssueKind,
    ReconcileService,
    RegisterDocument,
)

BACKEND_ROOT = Path(__file__).parents[3]
DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test",
)
TENANT_ID = UUID("01900000-0000-7000-8000-000000000501")
USER_ID = UUID("01900000-0000-7000-8000-000000000502")
COLLECTION_A = UUID("01900000-0000-7000-8000-000000000503")
COLLECTION_B = UUID("01900000-0000-7000-8000-000000000504")
ORPHAN_DOCUMENT = UUID("01900000-0000-7000-8000-000000000505")
ORPHAN_VERSION = UUID("01900000-0000-7000-8000-000000000506")
NOW = datetime(2026, 1, 1, tzinfo=UTC)
REVISION = "delete-reconcile-v1"


@pytest.fixture(scope="module", autouse=True)
def ensure_schema() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")


async def chunks(value: bytes) -> AsyncIterator[bytes]:
    yield value


async def seed_base(database: Database) -> None:
    async with database.session() as session:
        await session.execute(text("TRUNCATE TABLE users, tenants CASCADE"))
        session.add_all(
            [
                TenantModel(id=TENANT_ID, name="Lifecycle", slug="lifecycle"),
                UserModel(
                    id=USER_ID,
                    email="lifecycle@example.invalid",
                    password_hash="not-a-real-hash",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                CollectionModel(id=COLLECTION_A, tenant_id=TENANT_ID, name="Primary"),
                CollectionModel(id=COLLECTION_B, tenant_id=TENANT_ID, name="Secondary"),
            ]
        )


async def register_document(
    database: Database,
    store: LocalObjectStore,
    *,
    logical_name: str = "deletable",
    collection_id: UUID = COLLECTION_A,
    content: bytes = b"delete me",
) -> tuple[UUID, UUID, str]:
    result = await DocumentRegistrationService(database, store).register(
        RegisterDocument(
            tenant_id=TENANT_ID,
            collection_id=collection_id,
            created_by=USER_ID,
            logical_name=logical_name,
            title=logical_name,
            source_name=f"{logical_name}.txt",
            media_type="text/plain",
        ),
        chunks(content),
    )
    return result.document_id, result.version_id, result.object_key


async def add_ready_content_and_vector(
    database: Database,
    vector_store: MilvusLiteVectorStore,
    *,
    document_id: UUID,
    version_id: UUID,
) -> None:
    root_id = "root_" + "5" * 64
    leaf_id = "leaf_" + "6" * 64
    async with database.session() as session:
        document = await session.get(DocumentModel, document_id)
        version = await session.get(DocumentVersionModel, version_id)
        assert document is not None
        assert version is not None
        document.status = "ready"
        document.active_version_id = version_id
        version.status = "indexed"
        session.add(
            RootModel(
                id=root_id,
                tenant_id=TENANT_ID,
                document_id=document_id,
                version_id=version_id,
                index_revision=REVISION,
                ordinal=0,
                kind="text_block",
                source_locator={"line": 1},
                raw_text="delete me",
                clean_text="delete me",
                metadata_json={},
                content_hash="5" * 64,
            )
        )
        await session.flush()
        session.add(
            LeafModel(
                id=leaf_id,
                root_id=root_id,
                tenant_id=TENANT_ID,
                document_id=document_id,
                version_id=version_id,
                ordinal=0,
                text="delete me",
                retrieval_text="delete me",
                token_count=2,
                metadata_json={},
                content_hash="6" * 64,
            )
        )
    await vector_store.upsert(
        [
            VectorRecord(
                index_revision=REVISION,
                leaf_id=leaf_id,
                root_id=root_id,
                tenant_id=TENANT_ID,
                collection_id=COLLECTION_A,
                document_id=document_id,
                version_id=version_id,
                status="ready",
                dense_vector=(1.0, 0.0, 0.0),
                sparse_vector={1: 1.0},
            )
        ]
    )


async def start_delete_job(database: Database, job_id: UUID, *, owner: str = "deleter") -> None:
    async with database.session() as session:
        repository = IngestionJobRepository(session)
        leased = await repository.lease_next(
            owner=owner, now=NOW, lease_for=timedelta(minutes=5)
        )
        assert leased is not None
        assert leased.id == job_id
        await repository.start(job_id, owner=owner, now=NOW)


@pytest.mark.anyio
@pytest.mark.parametrize("crash_after", tuple(DeletionStep))
async def test_deletion_saga_restarts_idempotently_after_every_checkpoint(
    tmp_path: Path, crash_after: DeletionStep
) -> None:
    database = Database(DATABASE_URL)
    object_store = LocalObjectStore(tmp_path / "objects")
    vector_store = MilvusLiteVectorStore(tmp_path / "milvus.db")
    await vector_store.ensure_revision(IndexSchema(REVISION, 3))
    service = DocumentDeletionService(database, vector_store, object_store)
    try:
        await seed_base(database)
        document_id, version_id, object_key = await register_document(database, object_store)
        await add_ready_content_and_vector(
            database, vector_store, document_id=document_id, version_id=version_id
        )
        async with database.session() as session:
            ingest_job = await IngestionJobRepository(session).enqueue(
                tenant_id=TENANT_ID,
                document_id=document_id,
                version_id=version_id,
                available_at=NOW,
                max_attempts=3,
            )

        requested = await service.request_delete(
            tenant_id=TENANT_ID, document_id=document_id, now=NOW
        )
        repeated = await service.request_delete(
            tenant_id=TENANT_ID, document_id=document_id, now=NOW
        )
        assert repeated.job_id == requested.job_id
        assert repeated.already_requested
        async with database.session() as session:
            document = await session.get(DocumentModel, document_id)
            assert document is not None
            assert document.status == "deleting"
            assert document.active_version_id is None
            cancelled_ingest = await session.get(IngestionJobModel, ingest_job.id)
            assert cancelled_ingest is not None
            assert cancelled_ingest.status == "cancelled"

        await start_delete_job(database, requested.job_id)

        async def crash(step: DeletionStep) -> None:
            if step is crash_after:
                raise RuntimeError(f"crash after {step.value}")

        with pytest.raises(RuntimeError, match=crash_after.value):
            await service.execute(
                job_id=requested.job_id,
                owner="deleter",
                now=NOW + timedelta(seconds=1),
                checkpoint=crash,
            )

        completed = await service.execute(
            job_id=requested.job_id,
            owner="deleter",
            now=NOW + timedelta(seconds=2),
        )
        assert completed.document_id == document_id
        assert await vector_store.count_by_version(TENANT_ID, version_id) == 0
        assert not await object_store.exists(object_key)
        async with database.session() as session:
            document = await session.get(DocumentModel, document_id)
            version = await session.get(DocumentVersionModel, version_id)
            job = await session.get(IngestionJobModel, requested.job_id)
            assert document is not None and document.status == "deleted"
            assert version is not None and version.status == "deleted"
            assert job is not None and job.status == "succeeded"
            for model in (RootModel, LeafModel, DocumentContentClaimModel):
                count = await session.scalar(select(func.count()).select_from(model))
                assert count == 0

        repeated_completion = await service.execute(
            job_id=requested.job_id,
            owner="deleter",
            now=NOW + timedelta(seconds=3),
        )
        assert repeated_completion.already_completed
    finally:
        await vector_store.aclose()
        await database.dispose()


@pytest.mark.anyio
async def test_deleting_one_logical_owner_keeps_a_shared_object(tmp_path: Path) -> None:
    database = Database(DATABASE_URL)
    object_store = LocalObjectStore(tmp_path / "objects")
    vector_store = MilvusLiteVectorStore(tmp_path / "milvus.db")
    await vector_store.ensure_revision(IndexSchema(REVISION, 3))
    service = DocumentDeletionService(database, vector_store, object_store)
    try:
        await seed_base(database)
        first_document, _, object_key = await register_document(
            database, object_store, content=b"shared"
        )
        second_document, _, second_key = await register_document(
            database,
            object_store,
            logical_name="shared-copy",
            collection_id=COLLECTION_B,
            content=b"shared",
        )
        assert second_document != first_document
        assert second_key == object_key

        request = await service.request_delete(
            tenant_id=TENANT_ID, document_id=first_document, now=NOW
        )
        await start_delete_job(database, request.job_id)
        result = await service.execute(
            job_id=request.job_id, owner="deleter", now=NOW + timedelta(seconds=1)
        )

        assert result.object_count == 0
        assert await object_store.exists(object_key)
    finally:
        await vector_store.aclose()
        await database.dispose()


@pytest.mark.anyio
async def test_reconcile_reports_read_only_and_repairs_only_safe_orphans(
    tmp_path: Path,
) -> None:
    database = Database(DATABASE_URL)
    object_store = LocalObjectStore(tmp_path / "objects")
    vector_store = MilvusLiteVectorStore(tmp_path / "milvus.db")
    await vector_store.ensure_revision(IndexSchema(REVISION, 3))
    reconcile = ReconcileService(database, vector_store, object_store)
    try:
        await seed_base(database)
        document_id, version_id, missing_key = await register_document(database, object_store)
        await add_ready_content_and_vector(
            database, vector_store, document_id=document_id, version_id=version_id
        )
        await vector_store.delete_by_version(TENANT_ID, version_id)
        await object_store.delete(missing_key)
        orphan_object = await object_store.put(chunks(b"orphan-object"))
        await vector_store.upsert(
            [
                VectorRecord(
                    index_revision=REVISION,
                    leaf_id="leaf_" + "7" * 64,
                    root_id="root_" + "8" * 64,
                    tenant_id=TENANT_ID,
                    collection_id=COLLECTION_A,
                    document_id=ORPHAN_DOCUMENT,
                    version_id=ORPHAN_VERSION,
                    status="ready",
                    dense_vector=(0.0, 1.0, 0.0),
                    sparse_vector={2: 1.0},
                )
            ]
        )
        async with database.session() as session:
            job = await IngestionJobRepository(session).enqueue(
                tenant_id=TENANT_ID,
                document_id=document_id,
                version_id=version_id,
                available_at=NOW,
                max_attempts=2,
            )
        async with database.session() as session:
            repository = IngestionJobRepository(session)
            await repository.lease_next(
                owner="expired", now=NOW, lease_for=timedelta(seconds=1)
            )
            await repository.start(job.id, owner="expired", now=NOW)

        dry_run = await reconcile.run(now=NOW + timedelta(seconds=2))
        dry_kinds = {issue.kind for issue in dry_run.issues}
        assert dry_kinds == {
            ReconcileIssueKind.ORPHAN_VECTOR,
            ReconcileIssueKind.VECTOR_COUNT_MISMATCH,
            ReconcileIssueKind.MISSING_OBJECT,
            ReconcileIssueKind.ORPHAN_OBJECT,
            ReconcileIssueKind.EXPIRED_LEASE,
        }
        assert dry_run.repaired_count == 0
        assert await object_store.exists(orphan_object.key)
        assert await vector_store.count_by_version(TENANT_ID, ORPHAN_VERSION) == 1

        applied = await reconcile.run(now=NOW + timedelta(seconds=2), apply=True)
        assert applied.repaired_count == 3
        assert not await object_store.exists(orphan_object.key)
        assert await vector_store.count_by_version(TENANT_ID, ORPHAN_VERSION) == 0

        second_run = await reconcile.run(now=NOW + timedelta(seconds=2), apply=True)
        assert {issue.kind for issue in second_run.issues} == {
            ReconcileIssueKind.VECTOR_COUNT_MISMATCH,
            ReconcileIssueKind.MISSING_OBJECT,
        }
        assert second_run.repaired_count == 0
    finally:
        await vector_store.aclose()
        await database.dispose()
