import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from enterprise_rag.adapters.sparse import HashingSparseEncoder
from enterprise_rag.adapters.vector_store import MilvusLiteVectorStore
from enterprise_rag.domain import AppError, ErrorCode, LeafChunk, RootChunk, RootKind
from enterprise_rag.ports import (
    DenseSearchRequest,
    ProviderHealth,
    ProviderInfo,
    ProviderKind,
    SparseSearchRequest,
    UpsertResult,
    VectorProjection,
    VectorRecord,
    VectorStore,
)
from enterprise_rag.services import ProjectionRequest, ProjectionService

TENANT_ID = UUID("01900000-0000-7000-8000-000000001101")
COLLECTION_ID = UUID("01900000-0000-7000-8000-000000001102")
DOCUMENT_ID = UUID("01900000-0000-7000-8000-000000001103")
VERSION_ID = UUID("01900000-0000-7000-8000-000000001104")
REVISION = "projection-v1"


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
        return [[1.0, float(index), 0.1] for index, _ in enumerate(texts)]

    async def embed_query(self, text: str) -> list[float]:
        del text
        return [1.0, 0.0, 0.1]

    async def aclose(self) -> None:
        return None


class FailingVectorStore:
    def __init__(self) -> None:
        self.records: dict[str, VectorRecord] = {}
        self.upsert_calls = 0
        self.delete_calls = 0

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            ProviderKind.VECTOR_STORE,
            "failing",
            "1",
            frozenset({"upsert"}),
            False,
            ProviderHealth.HEALTHY,
        )

    async def ensure_revision(self, schema: object) -> None:
        del schema

    async def upsert(self, records: Sequence[VectorRecord]) -> UpsertResult:
        self.upsert_calls += 1
        if self.upsert_calls == 2:
            raise RuntimeError("vendor details")
        self.records.update({record.leaf_id: record for record in records})
        return UpsertResult(len(records))

    async def count_by_version(self, tenant_id: UUID, version_id: UUID) -> int:
        return sum(
            record.tenant_id == tenant_id and record.version_id == version_id
            for record in self.records.values()
        )

    async def delete_by_version(self, tenant_id: UUID, version_id: UUID) -> int:
        self.delete_calls += 1
        if self.delete_calls == 1:
            raise RuntimeError("temporary delete failure")
        matching = [
            key
            for key, record in self.records.items()
            if record.tenant_id == tenant_id and record.version_id == version_id
        ]
        for key in matching:
            self.records.pop(key)
        return len(matching)

    async def dense_search(self, request: object) -> list[object]:
        del request
        return []

    async def sparse_search(self, request: object) -> list[object]:
        del request
        return []

    async def list_version_projections(self) -> tuple[VectorProjection, ...]:
        return ()

    async def aclose(self) -> None:
        return None


def leaves() -> tuple[LeafChunk, ...]:
    root = RootChunk.create(
        tenant_id=TENANT_ID,
        document_id=DOCUMENT_ID,
        version_id=VERSION_ID,
        index_revision=REVISION,
        ordinal=0,
        kind=RootKind.SECTION,
        source_locator={"section": "test"},
        raw_text="企业 policy retrieval",
        clean_text="企业 policy retrieval",
    )
    values = ("企业知识库", "retrieval policy", "hybrid search")
    return tuple(
        LeafChunk.create(
            root_id=root.id,
            tenant_id=TENANT_ID,
            document_id=DOCUMENT_ID,
            version_id=VERSION_ID,
            ordinal=index,
            text=value,
            retrieval_text=value,
            start_offset=0,
            end_offset=len(value),
            token_count=max(1, len(value.split())),
        )
        for index, value in enumerate(values)
    )


def request() -> ProjectionRequest:
    return ProjectionRequest(
        TENANT_ID,
        COLLECTION_ID,
        DOCUMENT_ID,
        VERSION_ID,
        REVISION,
        leaves(),
    )


@pytest.mark.anyio
async def test_sparse_encoder_is_stable_multilingual_normalized_and_closable() -> None:
    encoder = HashingSparseEncoder()
    first = await encoder.encode_query("企业 Enterprise enterprise")
    repeated = await encoder.encode_query("企业 Enterprise enterprise")

    assert first == repeated
    assert len(first) == 3
    assert math.sqrt(sum(value * value for value in first.values())) == pytest.approx(1.0)
    with pytest.raises(AppError) as raised:
        await encoder.encode_query("!!!")
    assert raised.value.code is ErrorCode.EMBEDDING_INPUT_INVALID

    await encoder.aclose()
    await encoder.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await encoder.encode_query("query")


@pytest.mark.anyio
async def test_projection_batches_activates_verifies_and_is_idempotent(tmp_path: Path) -> None:
    store = MilvusLiteVectorStore(tmp_path / "projection.db")
    service = ProjectionService(
        embedding=FakeEmbedding(),
        sparse=HashingSparseEncoder(),
        vector_store=store,
        batch_size=2,
    )
    try:
        first = await service.project(request())
        second = await service.project(request())
        assert first.expected_count == first.staged_count == first.activated_count == 3
        assert first.verified_count == 3 and first.batches == 2
        assert second.verified_count == 3
        assert await store.count_by_version(TENANT_ID, VERSION_ID) == 3

        query_vector: Mapping[int, float] = await HashingSparseEncoder().encode_query("企业")
        hits = await store.sparse_search(SparseSearchRequest(REVISION, TENANT_ID, query_vector, 3))
        assert hits[0].leaf_id == leaves()[0].id
        dense = await store.dense_search(
            DenseSearchRequest(REVISION, TENANT_ID, (1.0, 0.0, 0.1), 3)
        )
        assert dense
    finally:
        await store.aclose()


@pytest.mark.anyio
async def test_partial_upsert_is_compensated_with_delete_retry_and_sanitized() -> None:
    store = FailingVectorStore()
    sleeps: list[float] = []

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)

    service = ProjectionService(
        embedding=FakeEmbedding(),
        sparse=HashingSparseEncoder(),
        vector_store=cast(VectorStore, store),
        batch_size=2,
        sleeper=sleeper,
    )
    with pytest.raises(AppError) as raised:
        await service.project(request())

    assert raised.value.code is ErrorCode.PROJECTION_UPSERT_FAILED
    assert "vendor details" not in str(raised.value)
    assert store.records == {}
    assert store.delete_calls == 2
    assert sleeps == [0.25]
