"""Stable interfaces between application services and replaceable adapters."""

from enterprise_rag.ports.object_store import (
    ObjectStore,
    StoredObject,
    object_key_for_sha256,
    validate_object_key,
    validate_sha256,
)
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
    "ObjectStore",
    "StoredObject",
    "object_key_for_sha256",
    "validate_object_key",
    "validate_sha256",
]
