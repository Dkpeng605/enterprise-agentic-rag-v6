"""Framework-neutral VectorStore contract and validated request records."""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

from enterprise_rag.domain.common import freeze_mapping, require_non_empty, require_uuid7
from enterprise_rag.ports.provider import Provider


def _validate_dense(vector: Sequence[float], *, dimension: int | None = None) -> None:
    if dimension is not None and len(vector) != dimension:
        raise ValueError("dense vector dimension does not match index schema")
    if not vector or any(not math.isfinite(value) for value in vector):
        raise ValueError("dense vectors must contain finite values")


def _validate_sparse(vector: Mapping[int, float]) -> None:
    if not vector:
        raise ValueError("sparse vectors must not be empty")
    if any(index < 0 or not math.isfinite(value) or value == 0 for index, value in vector.items()):
        raise ValueError("sparse vectors require non-negative indices and finite non-zero values")


@dataclass(frozen=True, slots=True)
class IndexSchema:
    revision: str
    dimension: int

    def __post_init__(self) -> None:
        require_non_empty(self.revision, "revision")
        if self.dimension <= 1:
            raise ValueError("dimension must be greater than one")


@dataclass(frozen=True, slots=True)
class VectorRecord:
    index_revision: str
    leaf_id: str
    root_id: str
    tenant_id: UUID
    collection_id: UUID
    document_id: UUID
    version_id: UUID
    status: str
    dense_vector: tuple[float, ...]
    sparse_vector: Mapping[int, float]
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_non_empty(self.index_revision, "index_revision")
        if not self.leaf_id.startswith("leaf_") or not self.root_id.startswith("root_"):
            raise ValueError("vector record IDs must use leaf_ and root_ prefixes")
        for name in ("tenant_id", "collection_id", "document_id", "version_id"):
            require_uuid7(getattr(self, name), name)
        if self.status not in {"processing", "ready", "deleting"}:
            raise ValueError("vector status is invalid")
        _validate_dense(self.dense_vector)
        _validate_sparse(self.sparse_vector)
        object.__setattr__(self, "sparse_vector", MappingProxyType(dict(self.sparse_vector)))
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class DenseSearchRequest:
    index_revision: str
    tenant_id: UUID
    vector: tuple[float, ...]
    top_k: int
    collection_ids: tuple[UUID, ...] = ()
    document_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        require_non_empty(self.index_revision, "index_revision")
        require_uuid7(self.tenant_id, "tenant_id")
        for field_name, values in (
            ("collection_ids", self.collection_ids),
            ("document_ids", self.document_ids),
        ):
            for value in values:
                require_uuid7(value, field_name)
        _validate_dense(self.vector)
        if not 1 <= self.top_k <= 50:
            raise ValueError("top_k must be between 1 and 50")


@dataclass(frozen=True, slots=True)
class SparseSearchRequest:
    index_revision: str
    tenant_id: UUID
    vector: Mapping[int, float]
    top_k: int
    collection_ids: tuple[UUID, ...] = ()
    document_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        require_non_empty(self.index_revision, "index_revision")
        require_uuid7(self.tenant_id, "tenant_id")
        for field_name, values in (
            ("collection_ids", self.collection_ids),
            ("document_ids", self.document_ids),
        ):
            for value in values:
                require_uuid7(value, field_name)
        _validate_sparse(self.vector)
        if not 1 <= self.top_k <= 50:
            raise ValueError("top_k must be between 1 and 50")
        object.__setattr__(self, "vector", MappingProxyType(dict(self.vector)))


@dataclass(frozen=True, slots=True)
class VectorHit:
    leaf_id: str
    root_id: str
    score: float
    metadata: Mapping[str, object]

    def __post_init__(self) -> None:
        if not math.isfinite(self.score):
            raise ValueError("vector hit score must be finite")
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class UpsertResult:
    count: int

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("upsert count must not be negative")


class VectorStore(Provider, Protocol):
    async def ensure_revision(self, schema: IndexSchema) -> None: ...

    async def upsert(self, records: Sequence[VectorRecord]) -> UpsertResult: ...

    async def dense_search(self, request: DenseSearchRequest) -> list[VectorHit]: ...

    async def sparse_search(self, request: SparseSearchRequest) -> list[VectorHit]: ...

    async def delete_by_version(self, tenant_id: UUID, version_id: UUID) -> int: ...

    async def count_by_version(self, tenant_id: UUID, version_id: UUID) -> int: ...
