from pathlib import Path
from uuid import UUID

import pytest

from enterprise_rag.adapters.vector_store import MilvusLiteVectorStore
from enterprise_rag.ports import (
    DenseSearchRequest,
    IndexSchema,
    SparseSearchRequest,
    VectorRecord,
)

REVISION = "test-revision-v1"
TENANT_A = UUID("01900000-0000-7000-8000-000000000301")
TENANT_B = UUID("01900000-0000-7000-8000-000000000302")
COLLECTION_A = UUID("01900000-0000-7000-8000-000000000303")
COLLECTION_B = UUID("01900000-0000-7000-8000-000000000304")
DOCUMENT_A = UUID("01900000-0000-7000-8000-000000000305")
DOCUMENT_B = UUID("01900000-0000-7000-8000-000000000306")
VERSION_A = UUID("01900000-0000-7000-8000-000000000307")
VERSION_B = UUID("01900000-0000-7000-8000-000000000308")


def record(
    suffix: str,
    *,
    tenant_id: UUID = TENANT_A,
    collection_id: UUID = COLLECTION_A,
    document_id: UUID = DOCUMENT_A,
    version_id: UUID = VERSION_A,
    dense: tuple[float, ...] = (1.0, 0.0, 0.0),
    sparse: dict[int, float] | None = None,
    status: str = "ready",
) -> VectorRecord:
    return VectorRecord(
        index_revision=REVISION,
        leaf_id=f"leaf_{suffix}",
        root_id=f"root_{suffix}",
        tenant_id=tenant_id,
        collection_id=collection_id,
        document_id=document_id,
        version_id=version_id,
        status=status,
        dense_vector=dense,
        sparse_vector=sparse or {1: 1.0},
        metadata={"label": suffix},
    )


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "milvus-contract.db"


@pytest.mark.anyio
async def test_milvus_lite_contract_covers_schema_upsert_search_filter_and_delete(
    database_path: Path,
) -> None:
    store = MilvusLiteVectorStore(database_path)
    await store.ensure_revision(IndexSchema(revision=REVISION, dimension=3))
    records = [
        record("tenant-a-one"),
        record(
            "tenant-a-two",
            collection_id=COLLECTION_B,
            document_id=DOCUMENT_B,
            version_id=VERSION_B,
            dense=(0.0, 1.0, 0.0),
            sparse={2: 1.0},
        ),
        record("processing", status="processing"),
        record("tenant-b", tenant_id=TENANT_B, sparse={1: 2.0}),
    ]
    try:
        result = await store.upsert(records)
        assert result.count == 4
        assert await store.count_by_version(TENANT_A, VERSION_A) == 2
        assert await store.count_by_version(TENANT_B, VERSION_A) == 1

        dense_hits = await store.dense_search(
            DenseSearchRequest(
                index_revision=REVISION,
                tenant_id=TENANT_A,
                vector=(1.0, 0.0, 0.0),
                top_k=10,
            )
        )
        assert [hit.leaf_id for hit in dense_hits] == ["leaf_tenant-a-one", "leaf_tenant-a-two"]
        assert all(hit.metadata["label"] != "tenant-b" for hit in dense_hits)
        assert all(hit.metadata["label"] != "processing" for hit in dense_hits)

        collection_hits = await store.dense_search(
            DenseSearchRequest(
                index_revision=REVISION,
                tenant_id=TENANT_A,
                vector=(1.0, 0.0, 0.0),
                top_k=10,
                collection_ids=(COLLECTION_A,),
            )
        )
        assert [hit.leaf_id for hit in collection_hits] == ["leaf_tenant-a-one"]

        sparse_hits = await store.sparse_search(
            SparseSearchRequest(
                index_revision=REVISION,
                tenant_id=TENANT_A,
                vector={2: 1.0},
                top_k=10,
            )
        )
        assert [hit.leaf_id for hit in sparse_hits] == ["leaf_tenant-a-two"]

        upserted = await store.upsert(
            [
                record(
                    "tenant-a-two",
                    collection_id=COLLECTION_B,
                    document_id=DOCUMENT_B,
                    version_id=VERSION_B,
                    dense=(0.0, 0.9, 0.1),
                    sparse={2: 0.9, 3: 0.1},
                )
            ]
        )
        assert upserted.count == 1
        assert await store.count_by_version(TENANT_A, VERSION_B) == 1

        assert await store.delete_by_version(TENANT_A, VERSION_A) == 2
        assert await store.count_by_version(TENANT_A, VERSION_A) == 0
        assert await store.count_by_version(TENANT_B, VERSION_A) == 1
    finally:
        await store.aclose()


@pytest.mark.anyio
async def test_revision_schema_is_persistent_and_rejects_dimension_change(
    database_path: Path,
) -> None:
    first = MilvusLiteVectorStore(database_path)
    await first.ensure_revision(IndexSchema(revision=REVISION, dimension=3))
    await first.upsert([record("persistent")])
    await first.aclose()

    reopened = MilvusLiteVectorStore(database_path)
    try:
        await reopened.ensure_revision(IndexSchema(revision=REVISION, dimension=3))
        assert await reopened.count_by_version(TENANT_A, VERSION_A) == 1
        with pytest.raises(ValueError, match="different dimension"):
            await reopened.ensure_revision(IndexSchema(revision=REVISION, dimension=4))
    finally:
        await reopened.aclose()


def test_vector_requests_reject_invalid_dimensions_sparse_values_and_top_k() -> None:
    with pytest.raises(ValueError, match="top_k"):
        DenseSearchRequest(REVISION, TENANT_A, (1.0, 0.0), 51)
    with pytest.raises(ValueError, match="finite non-zero"):
        SparseSearchRequest(REVISION, TENANT_A, {1: 0.0}, 5)
    with pytest.raises(ValueError, match="dimension"):
        IndexSchema(REVISION, 1)
