import os
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from enterprise_rag.adapters.database import (
    Database,
    PostgreSQLContextRepository,
    ScopeConflictError,
)
from enterprise_rag.adapters.database.models import (
    CollectionModel,
    DocumentModel,
    DocumentVersionModel,
    LeafModel,
    RootModel,
    TenantModel,
    UserModel,
)
from enterprise_rag.domain import ErrorCode, QueryScope
from enterprise_rag.ports import ScopeAuthorization

BACKEND_ROOT = Path(__file__).parents[3]
DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test",
)
TENANT_A = UUID("01900000-0000-7000-8000-000000001501")
TENANT_B = UUID("01900000-0000-7000-8000-000000001502")
USER_ID = UUID("01900000-0000-7000-8000-000000001503")
COLLECTION_A = UUID("01900000-0000-7000-8000-000000001504")
COLLECTION_B = UUID("01900000-0000-7000-8000-000000001505")
COLLECTION_OTHER = UUID("01900000-0000-7000-8000-000000001506")
DOCUMENT_A = UUID("01900000-0000-7000-8000-000000001507")
DOCUMENT_B = UUID("01900000-0000-7000-8000-000000001508")
DOCUMENT_DELETING = UUID("01900000-0000-7000-8000-000000001509")
DOCUMENT_OTHER = UUID("01900000-0000-7000-8000-00000000150a")
VERSION_A = UUID("01900000-0000-7000-8000-00000000150b")
VERSION_B = UUID("01900000-0000-7000-8000-00000000150c")
VERSION_DELETING = UUID("01900000-0000-7000-8000-00000000150d")
VERSION_OTHER = UUID("01900000-0000-7000-8000-00000000150e")
ROOT_A = "root_" + "a" * 64
ROOT_B = "root_" + "b" * 64
ROOT_DELETING = "root_" + "c" * 64
ROOT_OTHER = "root_" + "d" * 64
LEAF_A = "leaf_" + "a" * 64
LEAF_B = "leaf_" + "b" * 64
LEAF_DELETING = "leaf_" + "c" * 64
LEAF_OTHER = "leaf_" + "d" * 64


@pytest.fixture(scope="module", autouse=True)
def ensure_schema() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")


async def seed(database: Database) -> None:
    async with database.session() as session:
        await session.execute(text("TRUNCATE TABLE users, tenants CASCADE"))
        session.add_all(
            [
                TenantModel(id=TENANT_A, name="A", slug="scope-a"),
                TenantModel(id=TENANT_B, name="B", slug="scope-b"),
                UserModel(
                    id=USER_ID,
                    email="scope@example.invalid",
                    password_hash="not-a-real-hash",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                CollectionModel(id=COLLECTION_A, tenant_id=TENANT_A, name="Allowed"),
                CollectionModel(id=COLLECTION_B, tenant_id=TENANT_A, name="Restricted"),
                CollectionModel(id=COLLECTION_OTHER, tenant_id=TENANT_B, name="Other"),
            ]
        )
        await session.flush()
        documents = (
            (DOCUMENT_A, TENANT_A, COLLECTION_A, "Policy", "Acme", "ready", VERSION_A),
            (DOCUMENT_B, TENANT_A, COLLECTION_B, "Handbook", "Acme", "ready", VERSION_B),
            (
                DOCUMENT_DELETING,
                TENANT_A,
                COLLECTION_A,
                "Deleting",
                "Acme",
                "deleting",
                None,
            ),
            (DOCUMENT_OTHER, TENANT_B, COLLECTION_OTHER, "Other", "Other", "ready", VERSION_OTHER),
        )
        for document_id, tenant_id, collection_id, title, organization, status, _ in documents:
            session.add(
                DocumentModel(
                    id=document_id,
                    tenant_id=tenant_id,
                    collection_id=collection_id,
                    logical_name=title.casefold(),
                    title=title,
                    organization=organization,
                    status=status,
                    visibility="tenant",
                    created_by=USER_ID,
                )
            )
        await session.flush()
        versions = (
            (VERSION_A, DOCUMENT_A, "policy.txt"),
            (VERSION_B, DOCUMENT_B, "handbook.txt"),
            (VERSION_DELETING, DOCUMENT_DELETING, "deleting.txt"),
            (VERSION_OTHER, DOCUMENT_OTHER, "other.txt"),
        )
        for version_id, document_id, source_name in versions:
            session.add(
                DocumentVersionModel(
                    id=version_id,
                    document_id=document_id,
                    sha256=version_id.hex.ljust(64, "0"),
                    source_name=source_name,
                    media_type="text/plain",
                    size_bytes=20,
                    object_key=f"sha256/00/00/{version_id.hex.ljust(64, '0')}",
                    parser_provider="fixture",
                    parser_version="1",
                    status="indexed",
                )
            )
        await session.flush()
        for document_id, _, _, _, _, _, active_version in documents:
            document = await session.get(DocumentModel, document_id)
            assert document is not None
            document.active_version_id = active_version
        content = (
            (TENANT_A, DOCUMENT_A, VERSION_A, ROOT_A, LEAF_A, "Introduction", "policy evidence"),
            (TENANT_A, DOCUMENT_B, VERSION_B, ROOT_B, LEAF_B, "Private", "handbook evidence"),
            (
                TENANT_A,
                DOCUMENT_DELETING,
                VERSION_DELETING,
                ROOT_DELETING,
                LEAF_DELETING,
                "Introduction",
                "deleting evidence",
            ),
            (
                TENANT_B,
                DOCUMENT_OTHER,
                VERSION_OTHER,
                ROOT_OTHER,
                LEAF_OTHER,
                "Introduction",
                "other",
            ),
        )
        for tenant_id, document_id, version_id, root_id, leaf_id, section, value in content:
            session.add(
                RootModel(
                    id=root_id,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    version_id=version_id,
                    index_revision="scope-v1",
                    ordinal=0,
                    kind="section",
                    source_locator={"section": section},
                    raw_text=value,
                    clean_text=value,
                    metadata_json={},
                    content_hash="1" * 64,
                )
            )
            await session.flush()
            session.add(
                LeafModel(
                    id=leaf_id,
                    root_id=root_id,
                    tenant_id=tenant_id,
                    document_id=document_id,
                    version_id=version_id,
                    ordinal=0,
                    text=value,
                    retrieval_text=f"retrieval {value}",
                    token_count=2,
                    metadata_json={},
                    content_hash="2" * 64,
                )
            )


@pytest.mark.anyio
async def test_scope_metadata_permissions_root_recovery_and_stale_recheck() -> None:
    database = Database(DATABASE_URL)
    try:
        await seed(database)
        async with database.session() as session:
            repository = PostgreSQLContextRepository(session)
            authorization = ScopeAuthorization(TENANT_A, False, collection_ids=(COLLECTION_A,))
            requested = QueryScope(
                collection_ids=(COLLECTION_A,),
                document_ids=(DOCUMENT_A,),
                titles=("policy",),
                organizations=("ACME",),
                doc_types=("TEXT/PLAIN",),
                versions=(str(VERSION_A),),
                sections=("INTRODUCTION",),
            )
            scope = await repository.resolve_scope(authorization, requested)
            leaves = await repository.load_leaves(
                scope, (LEAF_A, LEAF_B, LEAF_DELETING, LEAF_OTHER)
            )
            roots = await repository.load_roots(scope, (ROOT_A, ROOT_B, ROOT_DELETING, ROOT_OTHER))

            assert scope.document_ids == (DOCUMENT_A,)
            assert [item.leaf_id for item in leaves] == [LEAF_A]
            assert [item.root_id for item in roots] == [ROOT_A]
            assert roots[0].text == "policy evidence"
            assert roots[0].source_locator == {"section": "Introduction"}

            with pytest.raises(ScopeConflictError) as conflict:
                await repository.resolve_scope(
                    authorization,
                    QueryScope(collection_ids=(COLLECTION_B,), document_ids=(DOCUMENT_A,)),
                )
            assert conflict.value.code is ErrorCode.QUERY_SCOPE_CONFLICT

            full_scope = await repository.resolve_scope(
                ScopeAuthorization(TENANT_A, True), QueryScope()
            )
            assert set(full_scope.document_ids) == {DOCUMENT_A, DOCUMENT_B}

        async with database.session() as session:
            document = await session.get(DocumentModel, DOCUMENT_A)
            assert document is not None
            document.status = "deleting"
            document.active_version_id = None

        async with database.session() as session:
            repository = PostgreSQLContextRepository(session)
            assert await repository.load_leaves(scope, (LEAF_A,)) == ()
            assert await repository.load_roots(scope, (ROOT_A,)) == ()
    finally:
        await database.dispose()
