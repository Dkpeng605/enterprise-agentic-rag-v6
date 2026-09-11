"""Independent dense and sparse retrieval with mandatory scope pushdown."""

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from enterprise_rag.domain.common import require_non_empty, require_uuid7
from enterprise_rag.domain.retrieval import QueryScope
from enterprise_rag.ports.embedding import EmbeddingProvider
from enterprise_rag.ports.sparse import SparseEncoder
from enterprise_rag.ports.vector_store import (
    DenseSearchRequest,
    SparseSearchRequest,
    VectorHit,
    VectorStore,
)


class SearchMethod(StrEnum):
    DENSE = "dense"
    SPARSE = "sparse"


@dataclass(frozen=True, slots=True)
class SearchDiagnostic:
    method: SearchMethod
    requested_top_k: int
    returned_count: int
    collection_filter_count: int
    document_filter_count: int


@dataclass(frozen=True, slots=True)
class SearchBranchResult:
    method: SearchMethod
    hits: tuple[VectorHit, ...]
    diagnostic: SearchDiagnostic


@dataclass(frozen=True, slots=True)
class DualSearchResult:
    query: str
    dense: SearchBranchResult
    sparse: SearchBranchResult


class DualSearchService:
    def __init__(
        self,
        *,
        embedding: EmbeddingProvider,
        sparse: SparseEncoder,
        vector_store: VectorStore,
        dense_top_k: int = 40,
        sparse_top_k: int = 40,
    ) -> None:
        for name, value in (("dense_top_k", dense_top_k), ("sparse_top_k", sparse_top_k)):
            if not 1 <= value <= 50:
                raise ValueError(f"{name} must be between 1 and 50")
        self._embedding = embedding
        self._sparse = sparse
        self._vector_store = vector_store
        self._dense_top_k = dense_top_k
        self._sparse_top_k = sparse_top_k

    async def search(
        self,
        *,
        query: str,
        tenant_id: UUID,
        index_revision: str,
        scope: QueryScope | None = None,
    ) -> DualSearchResult:
        require_non_empty(query, "query")
        require_non_empty(index_revision, "index_revision")
        require_uuid7(tenant_id, "tenant_id")
        active_scope = scope or QueryScope()
        dense_vector, sparse_vector = await asyncio.gather(
            self._embedding.embed_query(query), self._sparse.encode_query(query)
        )
        dense_request = DenseSearchRequest(
            index_revision=index_revision,
            tenant_id=tenant_id,
            vector=tuple(dense_vector),
            top_k=self._dense_top_k,
            collection_ids=active_scope.collection_ids,
            document_ids=active_scope.document_ids,
        )
        sparse_request = SparseSearchRequest(
            index_revision=index_revision,
            tenant_id=tenant_id,
            vector=sparse_vector,
            top_k=self._sparse_top_k,
            collection_ids=active_scope.collection_ids,
            document_ids=active_scope.document_ids,
        )
        dense_hits, sparse_hits = await asyncio.gather(
            self._vector_store.dense_search(dense_request),
            self._vector_store.sparse_search(sparse_request),
        )
        dense = self._branch(
            SearchMethod.DENSE,
            dense_hits,
            self._dense_top_k,
            active_scope,
        )
        sparse = self._branch(
            SearchMethod.SPARSE,
            sparse_hits,
            self._sparse_top_k,
            active_scope,
        )
        return DualSearchResult(query, dense, sparse)

    @staticmethod
    def _branch(
        method: SearchMethod,
        hits: list[VectorHit],
        top_k: int,
        scope: QueryScope,
    ) -> SearchBranchResult:
        if len(hits) > top_k:
            raise ValueError("VectorStore returned more hits than requested")
        identities = [hit.leaf_id for hit in hits]
        if len(identities) != len(set(identities)):
            raise ValueError("VectorStore returned duplicate leaf IDs in one branch")
        return SearchBranchResult(
            method,
            tuple(hits),
            SearchDiagnostic(
                method,
                top_k,
                len(hits),
                len(scope.collection_ids),
                len(scope.document_ids),
            ),
        )
