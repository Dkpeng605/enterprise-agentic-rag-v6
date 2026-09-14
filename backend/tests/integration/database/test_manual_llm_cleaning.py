import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from enterprise_rag.adapters.database import Database
from enterprise_rag.adapters.database.ingestion import IngestionContentRepository
from enterprise_rag.adapters.database.models import (
    CollectionModel,
    DocumentModel,
    DocumentVersionModel,
    RootModel,
    TenantModel,
    UserModel,
)
from enterprise_rag.adapters.embeddings import HashingDenseEmbedding
from enterprise_rag.adapters.sparse import HashingSparseEncoder
from enterprise_rag.adapters.splitters import StructureAwareSplitter
from enterprise_rag.adapters.vector_store import MilvusLiteVectorStore
from enterprise_rag.domain import AppError, ErrorCode, RootKind
from enterprise_rag.ports import (
    CleanRoot,
    CompletionRequest,
    CompletionResult,
    IngestionContext,
    ProviderHealth,
    ProviderInfo,
    ProviderKind,
)
from enterprise_rag.services import (
    ManualLlmCleaningService,
    ProjectionRequest,
    ProjectionService,
)

BACKEND_ROOT = Path(__file__).parents[3]
DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://enterprise_rag:enterprise_rag@127.0.0.1:55432/enterprise_rag_test",
)
TENANT_ID = UUID("01900000-0000-7000-8000-000000007201")
ACTOR_ID = UUID("01900000-0000-7000-8000-000000007202")
COLLECTION_ID = UUID("01900000-0000-7000-8000-000000007203")
DOCUMENT_ID = UUID("01900000-0000-7000-8000-000000007204")
VERSION_ID = UUID("01900000-0000-7000-8000-000000007205")
OTHER_TENANT_ID = UUID("01900000-0000-7000-8000-000000007206")
NOW = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)


class FakeCleaningLlm:
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            ProviderKind.LLM,
            "openai_compatible",
            "fixture-cleaner",
            frozenset({"chat-completions"}),
            True,
            ProviderHealth.HEALTHY,
        )

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        assert request.json_mode is True
        payload = json.loads(request.user_prompt)
        assert len(payload["roots"]) == 1
        return CompletionResult(
            json.dumps(
                {
                    "roots": [
                        {
                            "ordinal": 0,
                            "clean_text": "# 部署口令\n\nToken 是 2026。上传后  运行评测。",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            input_tokens=41,
            output_tokens=19,
        )

    async def aclose(self) -> None:
        return None


@pytest.fixture(scope="module", autouse=True)
def ensure_schema() -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.upgrade(config, "head")


@pytest.mark.anyio
async def test_confirmed_one_pass_rechunks_reprojects_and_persists_audit(
    tmp_path: Path,
) -> None:
    database = Database(DATABASE_URL)
    store = MilvusLiteVectorStore(tmp_path / "vectors.db")
    splitter = StructureAwareSplitter(target_tokens=10, max_tokens=16, overlap_tokens=2)
    projection = ProjectionService(
        embedding=HashingDenseEmbedding(dimension=8),
        sparse=HashingSparseEncoder(),
        vector_store=store,
        batch_size=4,
    )
    original = "# 部署口令\n\nToken 是 2026。\n\n\n上传后   运行评测。"
    context = IngestionContext(TENANT_ID, DOCUMENT_ID, VERSION_ID, tmp_path, "clean-v1")
    split = await splitter.split(
        CleanRoot(
            0,
            RootKind.SECTION,
            {"section": "deployment"},
            original,
            original,
            {
                "cleaning": {"provider": "deterministic", "version": "1", "audit": []}
            },
        ),
        context,
    )
    try:
        async with database.session() as session:
            await session.execute(text("TRUNCATE TABLE users, tenants CASCADE"))
            session.add_all(
                [
                    TenantModel(id=TENANT_ID, name="Cleaning", slug="cleaning"),
                    TenantModel(id=OTHER_TENANT_ID, name="Other", slug="other-cleaning"),
                    UserModel(
                        id=ACTOR_ID,
                        email="cleaner@example.test",
                        password_hash="fixture",
                        status="active",
                    ),
                ]
            )
            await session.flush()
            session.add(CollectionModel(id=COLLECTION_ID, tenant_id=TENANT_ID, name="Docs"))
            await session.flush()
            document = DocumentModel(
                id=DOCUMENT_ID,
                tenant_id=TENANT_ID,
                collection_id=COLLECTION_ID,
                logical_name="deployment.md",
                title="Deployment",
                status="pending",
                visibility="tenant",
                created_by=ACTOR_ID,
            )
            session.add(document)
            await session.flush()
            version = DocumentVersionModel(
                id=VERSION_ID,
                document_id=DOCUMENT_ID,
                sha256="a" * 64,
                source_name="deployment.md",
                media_type="text/markdown",
                size_bytes=len(original.encode()),
                object_key="fixture/deployment.md",
                parser_provider="text_documents",
                parser_version="1",
                status="indexed",
            )
            session.add(version)
            await session.flush()
            document.active_version_id = VERSION_ID
            document.status = "ready"
            await IngestionContentRepository(session).replace_content(
                version_id=VERSION_ID,
                roots=(split.root,),
                leaves=split.leaves,
                parser_provider="text_documents",
                parser_version="1",
            )
        await projection.project(
            ProjectionRequest(
                TENANT_ID,
                COLLECTION_ID,
                DOCUMENT_ID,
                VERSION_ID,
                "clean-v1",
                split.leaves,
            )
        )
        service = ManualLlmCleaningService(
            database=database,
            language_model=FakeCleaningLlm(),
            splitter=splitter,
            projection=projection,
            vector_store=store,
            temporary_root=tmp_path / "cleaning",
        )

        preflight = await service.preflight(TENANT_ID, DOCUMENT_ID)
        assert preflight.available is True
        assert preflight.estimated_calls == 1
        with pytest.raises(AppError) as unconfirmed:
            await service.clean(
                tenant_id=TENANT_ID,
                actor_id=ACTOR_ID,
                document_id=DOCUMENT_ID,
                expected_version_id=VERSION_ID,
                confirm_remote_processing=False,
                now=NOW,
            )
        assert unconfirmed.value.code is ErrorCode.VALIDATION_ERROR

        result = await service.clean(
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
            document_id=DOCUMENT_ID,
            expected_version_id=VERSION_ID,
            confirm_remote_processing=True,
            now=NOW,
        )

        assert result.llm_calls == 1
        assert result.changed_root_count == 1
        assert result.input_tokens == 41 and result.output_tokens == 19
        assert await store.count_by_version(TENANT_ID, VERSION_ID) == result.leaf_count_after
        async with database.session() as session:
            stored_document = await session.get(DocumentModel, DOCUMENT_ID)
            stored_version = await session.get(DocumentVersionModel, VERSION_ID)
            root = await session.scalar(select(RootModel).where(RootModel.version_id == VERSION_ID))
            assert stored_document is not None and stored_document.status == "ready"
            assert stored_version is not None and stored_version.status == "indexed"
            assert root is not None
            assert root.clean_text.endswith("上传后  运行评测。")
            assert root.raw_text == original
            assert root.metadata_json["llm_cleaning"]["model"] == "fixture-cleaner"
            assert root.metadata_json["cleaning"]["audit"][-1]["rule"] == "manual_llm_cleaning"

        repeated = await service.preflight(TENANT_ID, DOCUMENT_ID)
        assert repeated.available is False and repeated.already_applied is True
        with pytest.raises(AppError) as hidden:
            await service.preflight(OTHER_TENANT_ID, DOCUMENT_ID)
        assert hidden.value.code is ErrorCode.NOT_FOUND
    finally:
        await store.aclose()
        await database.dispose()
