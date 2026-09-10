"""Stable interfaces between application services and replaceable adapters."""

from enterprise_rag.ports.provider import (
    Provider,
    ProviderHealth,
    ProviderInfo,
    ProviderKind,
)
from enterprise_rag.ports.registry import ProviderRegistry, RegistryError, RegistryErrorCode
from enterprise_rag.ports.vector_store import (
    DenseSearchRequest,
    IndexSchema,
    SparseSearchRequest,
    UpsertResult,
    VectorHit,
    VectorRecord,
    VectorStore,
)

__all__ = [
    "Provider",
    "ProviderHealth",
    "ProviderInfo",
    "ProviderKind",
    "ProviderRegistry",
    "RegistryError",
    "RegistryErrorCode",
    "DenseSearchRequest",
    "IndexSchema",
    "SparseSearchRequest",
    "UpsertResult",
    "VectorHit",
    "VectorRecord",
    "VectorStore",
]
