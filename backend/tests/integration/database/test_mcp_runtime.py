"""End-to-end mounted MCP runtime with PostgreSQL, Milvus Lite, and official client."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx2
import pytest
from alembic import command
from alembic.config import Config
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextResourceContents
from sqlalchemy import text

from enterprise_rag.adapters.database import Database, PostgreSQLMcpTokenStore
from enterprise_rag.adapters.database.models import (
    CollectionModel,
    DocumentModel,
    DocumentVersionModel,
    LeafModel,
    MembershipModel,
    RootModel,
    TenantModel,
    UserModel,
)
from enterprise_rag.adapters.embeddings import HashingDenseEmbedding
from enterprise_rag.adapters.object_store import LocalObjectStore
from enterprise_rag.adapters.sparse import HashingSparseEncoder
from enterprise_rag.adapters.vector_store import MilvusLiteVectorStore
from enterprise_rag.api import create_app
from enterprise_rag.config import AppSettings
from enterprise_rag.mcp import build_http_mcp_app
from enterprise_rag.mcp.access import ALL_MCP_SCOPES
from enterprise_rag.mcp.catalog import McpCapabilityCatalog
from enterprise_rag.mcp.knowledge_catalog import McpKnowledgeCatalog
from enterprise_rag.mcp.tokens import McpTokenOperator
from enterprise_rag.ports import IndexSchema, VectorRecord
from enterprise_rag.services import (
    DualSearchService,
    McpApplicationService,
    QueryCommand,
    QueryExecution,
    QueryRunStatus,
    ReciprocalRankFusion,
)
from enterprise_rag.services.query_api import ProgressSink
from enterprise_rag.services.workspace import WorkspaceService

BACKEND_ROOT = Path(__file__).parents[3]
DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test",
)
NOW = datetime(2026, 9, 15, 3, 0, tzinfo=UTC)
PEPPER = "mounted-runtime-pepper-at-least-32-bytes"
SESSION_SECRET = "mounted-runtime-session-secret-at-least-32-bytes"
TENANT_ID = UUID("01900000-0000-7000-8000-000000005601")
ACTOR_ID = UUID("01900000-0000-7000-8000-000000005602")
COLLECTION_ID = UUID("01900000-0000-7000-8000-000000005603")
DOCUMENT_ID = UUID("01900000-0000-7000-8000-000000005604")
VERSION_ID = UUID("01900000-0000-7000-8000-000000005605")
OUTSIDE_COLLECTION_ID = UUID("01900000-0000-7000-8000-000000005606")
ROOT_ID = "root_" + "8" * 64
LEAF_ID = "leaf_" + "9" * 64
REVISION = "mcp-mounted-e2e-v1"


@pytest.fixture(scope="module", autouse=True)
def ensure_schema() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")


class RecordingRunner:
    def __init__(self) -> None:
        self.commands: list[QueryCommand] = []

    async def run(
        self, command: QueryCommand, *, emit: ProgressSink | None = None
    ) -> QueryExecution:
        del emit
        self.commands.append(command)
        return QueryExecution(
            command.query_id,
            QueryRunStatus.ANSWERED,
            "共享 KnowledgeApplication 已执行。",
            (),
            {"provider": "integration"},
            {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0},
        )


@pytest.mark.anyio
async def test_real_mounted_runtime_supports_catalog_search_resources_verify_and_query(
    tmp_path: Path,
) -> None:
    database = Database(DATABASE_URL)
    object_store = LocalObjectStore(tmp_path / "objects")
    embedding = HashingDenseEmbedding(dimension=32)
    sparse = HashingSparseEncoder()
    vector_store = MilvusLiteVectorStore(tmp_path / "milvus" / "vectors.db")
    runner = RecordingRunner()
    try:
        await _seed(database)
        dense = await embedding.embed_query("保留期限 三年 AB-120")
        sparse_vector = await sparse.encode_query("保留期限 三年 AB-120")
        await vector_store.ensure_revision(IndexSchema(REVISION, embedding.dimension))
        await vector_store.upsert(
            (
                VectorRecord(
                    REVISION,
                    LEAF_ID,
                    ROOT_ID,
                    TENANT_ID,
                    COLLECTION_ID,
                    DOCUMENT_ID,
                    VERSION_ID,
                    "ready",
                    tuple(dense),
                    sparse_vector,
                    {},
                ),
            )
        )
        workspace = WorkspaceService(
            database,
            object_store,
            max_upload_bytes=1024,
            max_documents=20,
            max_attempts=3,
        )
        catalog = McpKnowledgeCatalog(
            database=database,
            workspace=workspace,
            search=DualSearchService(
                embedding=embedding,
                sparse=sparse,
                vector_store=vector_store,
                dense_top_k=5,
                sparse_top_k=5,
            ),
            fusion=ReciprocalRankFusion(rrf_k=60, top_k=5),
            index_revision=REVISION,
        )
        issued = await McpTokenOperator(
            database, pepper=PEPPER, clock=lambda: NOW
        ).issue(
            tenant_slug="mcp-mounted-e2e",
            actor_email="mcp-mounted@example.invalid",
            name="official client e2e",
            collection_ids=(COLLECTION_ID,),
            scopes=tuple(sorted(ALL_MCP_SCOPES)),
            expires_in=timedelta(hours=1),
        )
        settings = AppSettings.model_validate(
            {
                "app": {
                    "public_base_url": "http://testserver",
                    "mcp_public_base_url": "http://testserver",
                }
            }
        )
        application = create_app(
            settings,
            database=database,
            object_store=object_store,
            session_secret=SESSION_SECRET,
            query_runner=runner,
            mcp_catalog=McpCapabilityCatalog(False, "http://testserver/mcp"),
            mcp_http_factory=lambda knowledge: build_http_mcp_app(
                McpApplicationService(knowledge),
                catalog,
                PostgreSQLMcpTokenStore(database),
                token_pepper=PEPPER,
                public_base_url="http://testserver",
                allow_insecure_http=True,
                clock=lambda: NOW,
            ),
            clock=lambda: NOW,
        )

        async with application.router.lifespan_context(application):
            plain = httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=application),
                base_url="http://testserver",
            )
            async with plain:
                unauthorized = await plain.post("/mcp")
            assert unauthorized.status_code == 401

            http = httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=application),
                base_url="http://testserver",
                headers={"authorization": f"Bearer {issued.raw_token}"},
            )
            async with http, Client(
                streamable_http_client(
                    "http://testserver/mcp", http_client=http, terminate_on_close=False
                )
            ) as client:
                collections = await client.call_tool("list_collections")
                searched = await client.call_tool(
                    "search_documents",
                    {"query": "保留期限 三年 AB-120", "strategy": "hybrid"},
                )
                summary = await client.call_tool(
                    "get_document_summary", {"document_id": str(DOCUMENT_ID)}
                )
                sections = await client.call_tool(
                    "list_document_sections", {"document_id": str(DOCUMENT_ID)}
                )
                resource = await client.read_resource(
                    f"rag://documents/{DOCUMENT_ID}/sections/{ROOT_ID}"
                )
                verified = await client.call_tool(
                    "verify_answer",
                    {
                        "answer": "保留期限为三年。",
                        "citations": [
                            {
                                "id": 1,
                                "document_id": str(DOCUMENT_ID),
                                "root_id": ROOT_ID,
                                "chunk_ids": [LEAF_ID],
                                "quote": "保留期限为三年",
                            }
                        ],
                    },
                )
                queried = await client.call_tool(
                    "query_knowledge_base", {"question": "保留期限是什么？"}
                )
                denied = await client.call_tool(
                    "query_knowledge_base",
                    {
                        "question": "越权查询",
                        "collection_ids": [str(OUTSIDE_COLLECTION_ID)],
                    },
                )

        assert collections.structured_content["items"][0]["id"] == str(COLLECTION_ID)
        assert searched.structured_content["items"][0]["leaf_id"] == LEAF_ID
        assert summary.structured_content["id"] == str(DOCUMENT_ID)
        assert sections.structured_content["items"][0]["id"] == ROOT_ID
        assert isinstance(resource.contents[0], TextResourceContents)
        assert "保留期限为三年" in resource.contents[0].text
        assert verified.structured_content["valid"] is True
        assert queried.structured_content["answer"] == "共享 KnowledgeApplication 已执行。"
        assert runner.commands[0].tenant_id == TENANT_ID
        assert runner.commands[0].scope.collection_ids == (COLLECTION_ID,)
        assert denied.is_error is True
        assert denied.structured_content["error"]["code"] == "FORBIDDEN"
    finally:
        await vector_store.aclose()
        await sparse.aclose()
        await embedding.aclose()
        await object_store.aclose()
        await database.dispose()


async def _seed(database: Database) -> None:
    async with database.session() as session:
        await session.execute(text("TRUNCATE TABLE users, tenants CASCADE"))
        session.add_all(
            [
                TenantModel(id=TENANT_ID, name="Mounted MCP", slug="mcp-mounted-e2e"),
                UserModel(
                    id=ACTOR_ID,
                    email="mcp-mounted@example.invalid",
                    password_hash="not-a-login-account",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                MembershipModel(tenant_id=TENANT_ID, user_id=ACTOR_ID, role="tenant_admin"),
                CollectionModel(
                    id=COLLECTION_ID,
                    tenant_id=TENANT_ID,
                    name="Retention policies",
                ),
            ]
        )
        await session.flush()
        session.add(
            DocumentModel(
                id=DOCUMENT_ID,
                tenant_id=TENANT_ID,
                collection_id=COLLECTION_ID,
                logical_name="retention-policy",
                title="Retention Policy",
                organization="Acme",
                status="ready",
                visibility="tenant",
                created_by=ACTOR_ID,
            )
        )
        await session.flush()
        session.add(
            DocumentVersionModel(
                id=VERSION_ID,
                document_id=DOCUMENT_ID,
                sha256="a" * 64,
                source_name="retention.txt",
                media_type="text/plain",
                size_bytes=128,
                object_key="sha256/aa/aa/" + "a" * 64,
                parser_provider="fixture",
                parser_version="1",
                status="indexed",
            )
        )
        await session.flush()
        document = await session.get(DocumentModel, DOCUMENT_ID)
        assert document is not None
        document.active_version_id = VERSION_ID
        session.add(
            RootModel(
                id=ROOT_ID,
                tenant_id=TENANT_ID,
                document_id=DOCUMENT_ID,
                version_id=VERSION_ID,
                index_revision=REVISION,
                ordinal=0,
                kind="section",
                source_locator={"section": "Retention"},
                raw_text="layout-noisy raw source",
                clean_text="保留期限为三年。合同编号 AB-120，应按顺序归档。",
                metadata_json={
                    "cleaning": {"provider": "fixture", "version": "1"},
                    "splitter": {"provider": "fixture", "version": "1"},
                },
                content_hash="b" * 64,
            )
        )
        await session.flush()
        session.add(
            LeafModel(
                id=LEAF_ID,
                root_id=ROOT_ID,
                tenant_id=TENANT_ID,
                document_id=DOCUMENT_ID,
                version_id=VERSION_ID,
                ordinal=0,
                text="保留期限为三年。合同编号 AB-120，应按顺序归档。",
                retrieval_text="保留期限 三年 合同编号 AB-120 顺序归档",
                token_count=12,
                metadata_json={},
                content_hash="c" * 64,
            )
        )
