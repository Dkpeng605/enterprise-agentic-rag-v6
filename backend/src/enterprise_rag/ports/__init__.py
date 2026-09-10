"""Stable interfaces between application services and replaceable adapters."""

from enterprise_rag.ports.cleaner import Cleaner, CleaningAudit, CleanResult, CleanRoot
from enterprise_rag.ports.loader import (
    BinarySource,
    IngestionContext,
    LoadedImage,
    LoadedRoot,
    Loader,
    OcrEngine,
)
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
from enterprise_rag.ports.splitter import SplitResult, Splitter
from enterprise_rag.ports.vector_store import (
    DenseSearchRequest,
    IndexSchema,
    SparseSearchRequest,
    UpsertResult,
    VectorHit,
    VectorProjection,
    VectorRecord,
    VectorStore,
)
from enterprise_rag.ports.vision import CaptionStatus, VisionImage, VisionProvider

__all__ = [
    "Cleaner",
    "CleaningAudit",
    "CleanResult",
    "CleanRoot",
    "Provider",
    "ProviderHealth",
    "ProviderInfo",
    "ProviderKind",
    "ProviderRegistry",
    "RegistryError",
    "RegistryErrorCode",
    "SplitResult",
    "Splitter",
    "DenseSearchRequest",
    "BinarySource",
    "IngestionContext",
    "IndexSchema",
    "SparseSearchRequest",
    "UpsertResult",
    "VectorHit",
    "VectorProjection",
    "VectorRecord",
    "VectorStore",
    "ObjectStore",
    "LoadedImage",
    "LoadedRoot",
    "Loader",
    "OcrEngine",
    "StoredObject",
    "object_key_for_sha256",
    "validate_object_key",
    "validate_sha256",
    "CaptionStatus",
    "VisionImage",
    "VisionProvider",
]
