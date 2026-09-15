"""Real PostgreSQL acceptance for the tenant-scoped MCP knowledge catalog."""

import os
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

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
from enterprise_rag.adapters.object_store import LocalObjectStore
from enterprise_rag.domain import ErrorCode
from enterprise_rag.domain.errors import AppError
from enterprise_rag.domain.retrieval import QueryScope
from enterprise_rag.mcp.knowledge_catalog import McpKnowledgeCatalog
from enterprise_rag.ports import VectorHit
from enterprise_rag.services import (
    DualSearchResult,
    Principal,
    ReciprocalRankFusion,
    SearchBranchResult,
    SearchDiagnostic,
    SearchMethod,
)
from enterprise_rag.services.workspace import WorkspaceService

BACKEND_ROOT = Path(__file__).parents[3]
DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test",
)
TENANT_ID = UUID("01900000-0000-7000-8000-000000005401")
ACTOR_ID = UUID("01900000-0000-7000-8000-000000005402")
TOKEN_ID = UUID("01900000-0000-7000-8000-000000005403")
ALLOWED_COLLECTION_ID = UUID("01900000-0000-7000-8000-000000005404")
DENIED_COLLECTION_ID = UUID("01900000-0000-7000-8000-000000005405")
ALLOWED_DOCUMENT_ID = UUID("01900000-0000-7000-8000-000000005406")
DENIED_DOCUMENT_ID = UUID("01900000-0000-7000-8000-000000005407")
ALLOWED_VERSION_ID = UUID("01900000-0000-7000-8000-000000005408")
DENIED_VERSION_ID = UUID("01900000-0000-7000-8000-000000005409")
ALLOWED_ROOT_ID = "root_" + "4" * 64
DENIED_ROOT_ID = "root_" + "5" * 64
ALLOWED_LEAF_ID = "leaf_" + "6" * 64
DENIED_LEAF_ID = "leaf_" + "7" * 64
REVISION = "mcp-catalog-v1"


@pytest.fixture(scope="module", autouse=True)
def ensure_schema() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")


class RecordingSearch:
    def __init__(self) -> None:
        self.scopes: list[QueryScope] = []

    async def search(
        self, *, query: str, tenant_id: UUID, index_revision: str, scope: QueryScope
    ) -> DualSearchResult:
        assert query == "保留期限"
        assert tenant_id == TENANT_ID
        assert index_revision == REVISION
        self.scopes.append(scope)
        dense = _branch(
            SearchMethod.DENSE,
            (
                VectorHit(ALLOWED_LEAF_ID, ALLOWED_ROOT_ID, 0.9, {"unsafe": "ignore"}),
                VectorHit(DENIED_LEAF_ID, DENIED_ROOT_ID, 0.8, {"unsafe": "ignore"}),
            ),
        )
        sparse = _branch(
            SearchMethod.SPARSE,
            (VectorHit(ALLOWED_LEAF_ID, ALLOWED_ROOT_ID, 2.0, {}),),
        )
        return DualSearchResult(query, dense, sparse)

    async def search_dense(
        self, *, query: str, tenant_id: UUID, index_revision: str, scope: QueryScope
    ) -> SearchBranchResult:
        return (await self.search(
            query=query,
            tenant_id=tenant_id,
            index_revision=index_revision,
            scope=scope,
        )).dense

    async def search_sparse(
        self, *, query: str, tenant_id: UUID, index_revision: str, scope: QueryScope
    ) -> SearchBranchResult:
        return (await self.search(
            query=query,
            tenant_id=tenant_id,
            index_revision=index_revision,
            scope=scope,
        )).sparse


def _branch(method: SearchMethod, hits: tuple[VectorHit, ...]) -> SearchBranchResult:
    return SearchBranchResult(method, hits, SearchDiagnostic(method, 10, len(hits), 1, 1))


@pytest.fixture
async def catalog(tmp_path: Path) -> AsyncIterator[tuple[McpKnowledgeCatalog, RecordingSearch]]:
    database = Database(DATABASE_URL)
    await _seed(database)
    workspace = WorkspaceService(
        database,
        LocalObjectStore(tmp_path / "objects"),
        max_upload_bytes=1024,
        max_documents=20,
        max_attempts=3,
    )
    search = RecordingSearch()
    instance = McpKnowledgeCatalog(
        database=database,
        workspace=workspace,
        search=search,
        fusion=ReciprocalRankFusion(rrf_k=60, top_k=20),
        index_revision=REVISION,
        max_snippet_chars=80,
        max_section_chars=16,
    )
    try:
        yield instance, search
    finally:
        await database.dispose()


def _principal() -> Principal:
    return Principal(TOKEN_ID, TENANT_ID, ACTOR_ID, actor_type="mcp_token", role="token")


@pytest.mark.anyio
async def test_metadata_resources_are_collection_scoped_and_bounded(
    catalog: tuple[McpKnowledgeCatalog, RecordingSearch],
) -> None:
    subject = catalog[0]
    collections = await subject.list_collections(
        _principal(), collection_ids=(ALLOWED_COLLECTION_ID,)
    )
    summary = await subject.get_document_summary(
        _principal(), ALLOWED_DOCUMENT_ID, collection_ids=(ALLOWED_COLLECTION_ID,)
    )
    sections = await subject.list_document_sections(
        _principal(),
        ALLOWED_DOCUMENT_ID,
        cursor=None,
        limit=20,
        collection_ids=(ALLOWED_COLLECTION_ID,),
    )
    section = await subject.get_section_resource(
        _principal(),
        ALLOWED_DOCUMENT_ID,
        ALLOWED_ROOT_ID,
        collection_ids=(ALLOWED_COLLECTION_ID,),
    )

    assert [item["id"] for item in collections] == [str(ALLOWED_COLLECTION_ID)]
    assert summary["id"] == str(ALLOWED_DOCUMENT_ID)
    assert summary["root_count"] == summary["leaf_count"] == 1
    section_items = cast(list[Mapping[str, object]], sections["items"])
    assert section_items[0]["id"] == ALLOWED_ROOT_ID
    assert "clean_text" not in repr(sections)
    assert "raw_text" not in repr(section)
    assert section["text"] == "保留期限为三年。合同编号 AB-120，应按原顺序归档。"[:16]
    assert section["truncated"] is True

    with pytest.raises(AppError) as denied:
        await subject.get_document_summary(
            _principal(), DENIED_DOCUMENT_ID, collection_ids=(ALLOWED_COLLECTION_ID,)
        )
    assert denied.value.code is ErrorCode.FORBIDDEN


@pytest.mark.anyio
async def test_search_pushes_authorized_documents_and_hydrates_only_postgresql_evidence(
    catalog: tuple[McpKnowledgeCatalog, RecordingSearch],
) -> None:
    subject, search = catalog
    result = await subject.search_documents(
        _principal(),
        query="保留期限",
        strategy="hybrid",
        top_k=10,
        filters={"sections": ["Retention"]},
        collection_ids=(ALLOWED_COLLECTION_ID,),
    )

    assert result["strategy"] == "hybrid"
    result_items = cast(list[Mapping[str, object]], result["items"])
    assert result_items[0]["leaf_id"] == ALLOWED_LEAF_ID
    assert result_items[0]["document_id"] == str(ALLOWED_DOCUMENT_ID)
    assert result_items[0]["snippet"] == "检索文本：保留期限为三年，合同编号 AB-120。"
    assert DENIED_LEAF_ID not in repr(result)
    assert search.scopes[0].document_ids == (ALLOWED_DOCUMENT_ID,)
    assert search.scopes[0].sections == ("retention",)

    with pytest.raises(AppError) as denied:
        await subject.search_documents(
            _principal(),
            query="保留期限",
            strategy="dense",
            top_k=10,
            filters={"collection_ids": [str(DENIED_COLLECTION_ID)]},
            collection_ids=(ALLOWED_COLLECTION_ID,),
        )
    assert denied.value.code is ErrorCode.FORBIDDEN


@pytest.mark.anyio
async def test_verification_checks_root_leaf_document_and_quote_without_echoing_input(
    catalog: tuple[McpKnowledgeCatalog, RecordingSearch],
) -> None:
    subject = catalog[0]
    valid = await subject.verify_answer(
        _principal(),
        answer="敏感答案不应回显",
        question="敏感问题不应回显",
        citations=(
            {
                "id": 1,
                "document_id": str(ALLOWED_DOCUMENT_ID),
                "root_id": ALLOWED_ROOT_ID,
                "chunk_ids": [ALLOWED_LEAF_ID],
                "quote": "保留期限为三年",
            },
        ),
        collection_ids=(ALLOWED_COLLECTION_ID,),
    )
    invalid = await subject.verify_answer(
        _principal(),
        answer="另一段敏感答案",
        question=None,
        citations=(
            {
                "id": 1,
                "document_id": str(ALLOWED_DOCUMENT_ID),
                "root_id": ALLOWED_ROOT_ID,
                "chunk_ids": [DENIED_LEAF_ID],
                "quote": "不存在的逐字引文",
            },
        ),
        collection_ids=(ALLOWED_COLLECTION_ID,),
    )

    assert valid == {"valid": True, "citation_count": 1, "verified_count": 1, "issues": []}
    assert invalid["valid"] is False
    issue_items = cast(list[Mapping[str, object]], invalid["issues"])
    assert {item["code"] for item in issue_items} == {
        "invalid_leaf",
        "quote_not_found",
    }
    assert "敏感" not in repr(valid) + repr(invalid)


async def _seed(database: Database) -> None:
    async with database.session() as session:
        await session.execute(text("TRUNCATE TABLE users, tenants CASCADE"))
        session.add(TenantModel(id=TENANT_ID, name="MCP Catalog", slug="mcp-catalog"))
        session.add(
            UserModel(
                id=ACTOR_ID,
                email="mcp-catalog@example.invalid",
                password_hash="not-a-login-account",
            )
        )
        await session.flush()
        session.add_all(
            [
                CollectionModel(
                    id=ALLOWED_COLLECTION_ID, tenant_id=TENANT_ID, name="Allowed"
                ),
                CollectionModel(
                    id=DENIED_COLLECTION_ID, tenant_id=TENANT_ID, name="Denied"
                ),
            ]
        )
        await session.flush()
        for document_id, collection_id, version_id, title in (
            (ALLOWED_DOCUMENT_ID, ALLOWED_COLLECTION_ID, ALLOWED_VERSION_ID, "Policy"),
            (DENIED_DOCUMENT_ID, DENIED_COLLECTION_ID, DENIED_VERSION_ID, "Secret"),
        ):
            session.add(
                DocumentModel(
                    id=document_id,
                    tenant_id=TENANT_ID,
                    collection_id=collection_id,
                    logical_name=title.casefold(),
                    title=title,
                    organization="Acme",
                    status="ready",
                    visibility="tenant",
                    created_by=ACTOR_ID,
                )
            )
            await session.flush()
            session.add(
                DocumentVersionModel(
                    id=version_id,
                    document_id=document_id,
                    sha256=version_id.hex.ljust(64, "0"),
                    source_name=f"{title.casefold()}.txt",
                    media_type="text/plain",
                    size_bytes=128,
                    object_key=f"sha256/00/00/{version_id.hex.ljust(64, '0')}",
                    parser_provider="fixture",
                    parser_version="1",
                    status="indexed",
                )
            )
            await session.flush()
            model = await session.get(DocumentModel, document_id)
            assert model is not None
            model.active_version_id = version_id
        await session.flush()
        for document_id, version_id, root_id, leaf_id, value in (
            (
                ALLOWED_DOCUMENT_ID,
                ALLOWED_VERSION_ID,
                ALLOWED_ROOT_ID,
                ALLOWED_LEAF_ID,
                "保留期限为三年。合同编号 AB-120，应按原顺序归档。",
            ),
            (
                DENIED_DOCUMENT_ID,
                DENIED_VERSION_ID,
                DENIED_ROOT_ID,
                DENIED_LEAF_ID,
                "不得泄露的内容。",
            ),
        ):
            session.add(
                RootModel(
                    id=root_id,
                    tenant_id=TENANT_ID,
                    document_id=document_id,
                    version_id=version_id,
                    index_revision=REVISION,
                    ordinal=0,
                    kind="section",
                    source_locator={"section": "Retention"},
                    raw_text="raw must never leave MCP",
                    clean_text=value,
                    metadata_json={
                        "cleaning": {"provider": "fixture", "version": "1"},
                        "splitter": {"provider": "fixture", "version": "1"},
                    },
                    content_hash="1" * 64,
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
                    text=value,
                    retrieval_text=(
                        "检索文本：保留期限为三年，合同编号 AB-120。"
                        if document_id == ALLOWED_DOCUMENT_ID
                        else "不得泄露"
                    ),
                    token_count=16,
                    metadata_json={},
                    content_hash="2" * 64,
                )
            )
