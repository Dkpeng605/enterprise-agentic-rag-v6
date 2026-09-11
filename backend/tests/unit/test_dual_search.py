from collections.abc import Mapping, Sequence
from uuid import UUID

import pytest

from enterprise_rag.domain import QueryScope
from enterprise_rag.ports import (
    DenseSearchRequest,
    ProviderHealth,
    ProviderInfo,
    ProviderKind,
    SparseSearchRequest,
    UpsertResult,
    VectorHit,
    VectorProjection,
)
from enterprise_rag.services import DualSearchService, SearchMethod

TENANT_ID = UUID("01900000-0000-7000-8000-000000002001")
COLLECTION_ID = UUID("01900000-0000-7000-8000-000000002002")
DOCUMENT_ID = UUID("01900000-0000-7000-8000-000000002003")


class FakeEmbedding:
    dimension = 3

    def __init__(self) -> None:
        self.queries: list[str] = []

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            ProviderKind.EMBEDDING, "fake", "1", frozenset({"query"}), False, ProviderHealth.HEALTHY
        )

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return [1.0, 0.5, 0.25]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        raise AssertionError(f"document embedding must not run during search: {texts}")

    async def aclose(self) -> None:
        return None


class FakeSparse:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            ProviderKind.SPARSE_ENCODER,
            "fake",
            "1",
            frozenset({"query"}),
            False,
            ProviderHealth.HEALTHY,
        )

    async def encode_query(self, text: str) -> Mapping[int, float]:
        self.queries.append(text)
        return {7: 1.0}

    async def encode_documents(self, texts: Sequence[str]) -> list[Mapping[int, float]]:
        raise AssertionError(f"document sparse encoding must not run during search: {texts}")

    async def aclose(self) -> None:
        return None


class SpyVectorStore:
    def __init__(
        self, dense_hits: list[VectorHit] | None = None, sparse_hits: list[VectorHit] | None = None
    ) -> None:
        self.dense_hits = dense_hits or []
        self.sparse_hits = sparse_hits or []
        self.dense_requests: list[DenseSearchRequest] = []
        self.sparse_requests: list[SparseSearchRequest] = []

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            ProviderKind.VECTOR_STORE,
            "spy",
            "1",
            frozenset({"dense", "sparse"}),
            False,
            ProviderHealth.HEALTHY,
        )

    async def dense_search(self, request: DenseSearchRequest) -> list[VectorHit]:
        self.dense_requests.append(request)
        return self.dense_hits

    async def sparse_search(self, request: SparseSearchRequest) -> list[VectorHit]:
        self.sparse_requests.append(request)
        return self.sparse_hits

    async def ensure_revision(self, schema: object) -> None:
        del schema

    async def upsert(self, records: Sequence[object]) -> UpsertResult:
        return UpsertResult(len(records))

    async def delete_by_version(self, tenant_id: UUID, version_id: UUID) -> int:
        del tenant_id, version_id
        return 0

    async def count_by_version(self, tenant_id: UUID, version_id: UUID) -> int:
        del tenant_id, version_id
        return 0

    async def list_version_projections(self) -> tuple[VectorProjection, ...]:
        return ()

    async def aclose(self) -> None:
        return None


def hit(identity: str, root: str, score: float) -> VectorHit:
    return VectorHit(f"leaf_{identity * 64}"[:69], f"root_{root * 64}"[:69], score, {})


@pytest.mark.anyio
async def test_dense_and_sparse_are_independent_and_scope_is_pushed_before_search() -> None:
    embedding = FakeEmbedding()
    sparse = FakeSparse()
    store = SpyVectorStore([hit("a", "1", 0.9)], [hit("b", "2", 2.0)])
    service = DualSearchService(
        embedding=embedding,
        sparse=sparse,
        vector_store=store,
        dense_top_k=3,
        sparse_top_k=2,
    )

    result = await service.search(
        query="企业安全政策",
        tenant_id=TENANT_ID,
        index_revision="rev-1",
        scope=QueryScope(collection_ids=(COLLECTION_ID,), document_ids=(DOCUMENT_ID,)),
    )

    assert embedding.queries == sparse.queries == ["企业安全政策"]
    assert result.dense.method is SearchMethod.DENSE
    assert result.sparse.method is SearchMethod.SPARSE
    assert [item.leaf_id for item in result.dense.hits] != [
        item.leaf_id for item in result.sparse.hits
    ]
    dense_request = store.dense_requests[0]
    sparse_request = store.sparse_requests[0]
    for request in (dense_request, sparse_request):
        assert request.tenant_id == TENANT_ID
        assert request.collection_ids == (COLLECTION_ID,)
        assert request.document_ids == (DOCUMENT_ID,)
    assert dense_request.top_k == 3
    assert sparse_request.top_k == 2
    assert result.dense.diagnostic.returned_count == 1
    assert result.sparse.diagnostic.document_filter_count == 1


@pytest.mark.anyio
async def test_empty_results_remain_two_explicit_successful_branches() -> None:
    service = DualSearchService(
        embedding=FakeEmbedding(), sparse=FakeSparse(), vector_store=SpyVectorStore()
    )

    result = await service.search(query="no match", tenant_id=TENANT_ID, index_revision="rev-empty")

    assert result.dense.hits == result.sparse.hits == ()
    assert result.dense.diagnostic.returned_count == 0
    assert result.sparse.diagnostic.returned_count == 0


@pytest.mark.parametrize("dense,sparse", [(0, 40), (51, 40), (40, 0), (40, 51)])
def test_top_k_is_bounded_before_provider_calls(dense: int, sparse: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 50"):
        DualSearchService(
            embedding=FakeEmbedding(),
            sparse=FakeSparse(),
            vector_store=SpyVectorStore(),
            dense_top_k=dense,
            sparse_top_k=sparse,
        )


@pytest.mark.anyio
async def test_duplicate_or_excessive_store_results_are_rejected() -> None:
    duplicate = hit("c", "3", 1.0)
    duplicate_store = SpyVectorStore([duplicate, duplicate], [])
    service = DualSearchService(
        embedding=FakeEmbedding(),
        sparse=FakeSparse(),
        vector_store=duplicate_store,
        dense_top_k=2,
    )
    with pytest.raises(ValueError, match="duplicate"):
        await service.search(query="duplicate", tenant_id=TENANT_ID, index_revision="rev")

    excessive_store = SpyVectorStore([hit(str(i), "4", 1.0) for i in range(4)], [])
    service = DualSearchService(
        embedding=FakeEmbedding(),
        sparse=FakeSparse(),
        vector_store=excessive_store,
        dense_top_k=3,
    )
    with pytest.raises(ValueError, match="more hits"):
        await service.search(query="excessive", tenant_id=TENANT_ID, index_revision="rev")
