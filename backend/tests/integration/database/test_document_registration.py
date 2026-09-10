import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text

from enterprise_rag.adapters.database import Database, DocumentRegistrationError
from enterprise_rag.adapters.database.models import (
    CollectionModel,
    DocumentContentClaimModel,
    DocumentModel,
    DocumentVersionModel,
    TenantModel,
    UserModel,
)
from enterprise_rag.adapters.object_store import LocalObjectStore
from enterprise_rag.domain import ErrorCode
from enterprise_rag.services import DocumentRegistrationService, RegisterDocument

BACKEND_ROOT = Path(__file__).parents[3]
DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test",
)
TENANT_A = UUID("01900000-0000-7000-8000-000000000401")
TENANT_B = UUID("01900000-0000-7000-8000-000000000402")
USER_A = UUID("01900000-0000-7000-8000-000000000403")
USER_B = UUID("01900000-0000-7000-8000-000000000404")
COLLECTION_A1 = UUID("01900000-0000-7000-8000-000000000405")
COLLECTION_A2 = UUID("01900000-0000-7000-8000-000000000406")
COLLECTION_B1 = UUID("01900000-0000-7000-8000-000000000407")


@pytest.fixture(scope="module", autouse=True)
def ensure_schema() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")


async def payload(value: bytes) -> AsyncIterator[bytes]:
    midpoint = len(value) // 2
    yield value[:midpoint]
    yield value[midpoint:]


def registration_command(
    *,
    tenant_id: UUID = TENANT_A,
    collection_id: UUID = COLLECTION_A1,
    created_by: UUID = USER_A,
    logical_name: str = "handbook",
    source_name: str = "../display-only.txt",
) -> RegisterDocument:
    return RegisterDocument(
        tenant_id=tenant_id,
        collection_id=collection_id,
        created_by=created_by,
        logical_name=logical_name,
        title="Employee handbook",
        source_name=source_name,
        media_type="text/plain",
    )


async def seed(database: Database) -> None:
    async with database.session() as session:
        await session.execute(text("TRUNCATE TABLE users, tenants CASCADE"))
        session.add_all(
            [
                TenantModel(id=TENANT_A, name="Tenant A", slug="registration-a"),
                TenantModel(id=TENANT_B, name="Tenant B", slug="registration-b"),
                UserModel(
                    id=USER_A,
                    email="registration-a@example.invalid",
                    password_hash="not-a-real-hash",
                ),
                UserModel(
                    id=USER_B,
                    email="registration-b@example.invalid",
                    password_hash="not-a-real-hash",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                CollectionModel(
                    id=COLLECTION_A1, tenant_id=TENANT_A, name="Tenant A / One"
                ),
                CollectionModel(
                    id=COLLECTION_A2, tenant_id=TENANT_A, name="Tenant A / Two"
                ),
                CollectionModel(
                    id=COLLECTION_B1, tenant_id=TENANT_B, name="Tenant B / One"
                ),
            ]
        )


@pytest.mark.anyio
async def test_same_tenant_collection_and_hash_returns_existing_registration(
    tmp_path: Path,
) -> None:
    database = Database(DATABASE_URL)
    store = LocalObjectStore(tmp_path / "objects")
    service = DocumentRegistrationService(database, store)
    try:
        await seed(database)

        first = await service.register(registration_command(), payload(b"same-content"))
        duplicate = await service.register(
            registration_command(logical_name="ignored-duplicate-name"),
            payload(b"same-content"),
        )

        assert first.deduplicated is False
        assert first.new_document is True
        assert duplicate.deduplicated is True
        assert duplicate.document_id == first.document_id
        assert duplicate.version_id == first.version_id
        assert duplicate.object_key == first.object_key
        async with database.session() as session:
            version = await session.get(DocumentVersionModel, first.version_id)
            assert version is not None
            assert version.source_name == "../display-only.txt"
            assert version.object_key.startswith("sha256/")
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_same_hash_has_independent_logical_ownership_across_collection_and_tenant(
    tmp_path: Path,
) -> None:
    database = Database(DATABASE_URL)
    service = DocumentRegistrationService(database, LocalObjectStore(tmp_path / "objects"))
    try:
        await seed(database)

        first = await service.register(registration_command(), payload(b"shared-object"))
        other_collection = await service.register(
            registration_command(collection_id=COLLECTION_A2), payload(b"shared-object")
        )
        other_tenant = await service.register(
            registration_command(
                tenant_id=TENANT_B,
                collection_id=COLLECTION_B1,
                created_by=USER_B,
            ),
            payload(b"shared-object"),
        )

        assert len(
            {first.document_id, other_collection.document_id, other_tenant.document_id}
        ) == 3
        assert len({first.version_id, other_collection.version_id, other_tenant.version_id}) == 3
        assert {first.object_key, other_collection.object_key, other_tenant.object_key} == {
            first.object_key
        }
        assert not other_collection.deduplicated
        assert not other_tenant.deduplicated
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_new_hash_for_logical_document_creates_a_new_version(tmp_path: Path) -> None:
    database = Database(DATABASE_URL)
    service = DocumentRegistrationService(database, LocalObjectStore(tmp_path / "objects"))
    try:
        await seed(database)

        first = await service.register(registration_command(), payload(b"version-one"))
        second = await service.register(registration_command(), payload(b"version-two"))

        assert second.document_id == first.document_id
        assert second.version_id != first.version_id
        assert second.new_document is False
        assert second.deduplicated is False
        async with database.session() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(DocumentVersionModel)
                .where(DocumentVersionModel.document_id == first.document_id)
            )
            document = await session.get(DocumentModel, first.document_id)
            assert count == 2
            assert document is not None
            assert document.status == "processing"
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_concurrent_duplicate_upload_has_one_document_version_and_claim(
    tmp_path: Path,
) -> None:
    database = Database(DATABASE_URL, pool_size=10, max_overflow=0)
    store = LocalObjectStore(tmp_path / "objects")
    service = DocumentRegistrationService(database, store)
    try:
        await seed(database)

        results = await asyncio.gather(
            *(
                service.register(registration_command(), payload(b"concurrent-upload"))
                for _ in range(8)
            )
        )

        assert len({result.document_id for result in results}) == 1
        assert len({result.version_id for result in results}) == 1
        assert sum(not result.deduplicated for result in results) == 1
        async with database.session() as session:
            counts = []
            for model in (
                DocumentModel,
                DocumentVersionModel,
                DocumentContentClaimModel,
            ):
                counts.append(await session.scalar(select(func.count()).select_from(model)))
            assert counts == [1, 1, 1]
        assert len(await store.list_keys()) == 1
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_collection_ownership_is_checked_inside_the_tenant_boundary(
    tmp_path: Path,
) -> None:
    database = Database(DATABASE_URL)
    service = DocumentRegistrationService(database, LocalObjectStore(tmp_path / "objects"))
    try:
        await seed(database)

        with pytest.raises(DocumentRegistrationError) as raised:
            await service.register(
                registration_command(collection_id=COLLECTION_B1), payload(b"not-owned")
            )

        assert raised.value.code is ErrorCode.NOT_FOUND
        assert raised.value.message == "The collection was not found."
    finally:
        await database.dispose()
