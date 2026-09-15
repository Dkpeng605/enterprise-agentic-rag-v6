from pathlib import Path
from uuid import UUID

import pytest

from enterprise_rag.adapters.sparse import MilvusBuiltinBm25Encoder
from enterprise_rag.adapters.vector_store import MilvusLiteVectorStore
from enterprise_rag.ports import (
    IndexSchema,
    SparseMode,
    SparseSearchRequest,
    VectorRecord,
)

REVISION = "bm25-jieba-v1"
TENANT_A = UUID("01900000-0000-7000-8000-000000009001")
TENANT_B = UUID("01900000-0000-7000-8000-000000009002")
COLLECTION_A = UUID("01900000-0000-7000-8000-000000009003")
COLLECTION_B = UUID("01900000-0000-7000-8000-000000009004")
DOCUMENT_A = UUID("01900000-0000-7000-8000-000000009005")
DOCUMENT_B = UUID("01900000-0000-7000-8000-000000009006")
VERSION_A = UUID("01900000-0000-7000-8000-000000009007")


def bm25_record(
    suffix: str,
    text: str,
    *,
    tenant_id: UUID = TENANT_A,
    collection_id: UUID = COLLECTION_A,
    document_id: UUID = DOCUMENT_A,
) -> VectorRecord:
    return VectorRecord(
        index_revision=REVISION,
        leaf_id=f"leaf_{suffix}",
        root_id=f"root_{suffix}",
        tenant_id=tenant_id,
        collection_id=collection_id,
        document_id=document_id,
        version_id=VERSION_A,
        status="ready",
        dense_vector=(1.0, 0.0, 0.0),
        retrieval_text=text,
        metadata={"label": suffix},
    )


@pytest.mark.anyio
async def test_bm25_provider_returns_explicit_text_backed_representations() -> None:
    provider = MilvusBuiltinBm25Encoder()
    documents = await provider.encode_documents(("信息安全管理制度", "Policy AB-120"))
    query = await provider.encode_query("安全制度 AB-120")

    assert provider.mode is SparseMode.MILVUS_BUILTIN_BM25
    assert provider.info().name == "milvus_builtin_bm25"
    assert [item.text for item in documents] == ["信息安全管理制度", "Policy AB-120"]
    assert all(item.vector is None for item in documents)
    assert query.text == "安全制度 AB-120"
    assert query.vector is None


@pytest.mark.anyio
async def test_milvus_builtin_bm25_recalls_chinese_identifiers_and_mixed_text(
    tmp_path: Path,
) -> None:
    store = MilvusLiteVectorStore(tmp_path / "bm25.db")
    schema = IndexSchema(REVISION, 3, sparse_mode=SparseMode.MILVUS_BUILTIN_BM25)
    await store.ensure_revision(schema)
    try:
        await store.upsert(
            [
                bm25_record("security", "信息安全管理制度要求每季度审计，编号 AB-120。"),
                bm25_record(
                    "finance",
                    "财务报销流程使用编号 FIN-9。",
                    collection_id=COLLECTION_B,
                    document_id=DOCUMENT_B,
                ),
                bm25_record(
                    "other-tenant",
                    "信息安全管理制度 AB-120",
                    tenant_id=TENANT_B,
                ),
            ]
        )

        chinese = await store.sparse_search(
            SparseSearchRequest(
                REVISION,
                TENANT_A,
                None,
                top_k=10,
                query_text="安全管理制度",
            )
        )
        assert [hit.leaf_id for hit in chinese] == ["leaf_security"]

        identifier = await store.sparse_search(
            SparseSearchRequest(
                REVISION,
                TENANT_A,
                None,
                top_k=10,
                query_text="AB-120",
                collection_ids=(COLLECTION_A,),
                document_ids=(DOCUMENT_A,),
            )
        )
        assert [hit.leaf_id for hit in identifier] == ["leaf_security"]
    finally:
        await store.aclose()


@pytest.mark.anyio
async def test_bm25_schema_mode_survives_reopen_and_rejects_mode_mismatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bm25-reopen.db"
    bm25_schema = IndexSchema(REVISION, 3, sparse_mode=SparseMode.MILVUS_BUILTIN_BM25)
    first = MilvusLiteVectorStore(path)
    await first.ensure_revision(bm25_schema)
    await first.upsert([bm25_record("persistent", "重启后仍可检索的安全制度")])
    await first.aclose()

    reopened = MilvusLiteVectorStore(path)
    try:
        await reopened.ensure_revision(bm25_schema)
        hits = await reopened.sparse_search(
            SparseSearchRequest(
                REVISION,
                TENANT_A,
                None,
                top_k=5,
                query_text="安全制度",
            )
        )
        assert [hit.leaf_id for hit in hits] == ["leaf_persistent"]
        with pytest.raises(ValueError, match="different sparse mode"):
            await reopened.ensure_revision(
                IndexSchema(REVISION, 3, sparse_mode=SparseMode.PRECOMPUTED)
            )
    finally:
        await reopened.aclose()
