"""Common lifecycle and metadata contract for every pluggable provider."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class ProviderKind(StrEnum):
    """Stable extension points across ingestion, retrieval, generation, and evaluation."""

    LOADER = "loader"
    OCR = "ocr"
    CLEANER = "cleaner"
    SPLITTER = "splitter"
    EMBEDDING = "embedding"
    VECTOR_STORE = "vector_store"
    OBJECT_STORE = "object_store"
    RERANKER = "reranker"
    LLM = "llm"
    EVALUATOR = "evaluator"
    TRACE_EXPORTER = "trace_exporter"


class ProviderHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    """Serializable identity and capability metadata used for discovery."""

    kind: ProviderKind
    name: str
    version: str
    capabilities: frozenset[str]
    is_remote: bool
    health: ProviderHealth = ProviderHealth.UNKNOWN

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("provider name must not be empty")
        if not self.version.strip():
            raise ValueError("provider version must not be empty")
        if any(not capability.strip() for capability in self.capabilities):
            raise ValueError("provider capabilities must not contain empty values")

    @property
    def key(self) -> tuple[ProviderKind, str]:
        return (self.kind, self.name)

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic API-safe representation."""

        return {
            "kind": self.kind.value,
            "name": self.name,
            "version": self.version,
            "capabilities": sorted(self.capabilities),
            "is_remote": self.is_remote,
            "health": self.health.value,
        }


@runtime_checkable
class Provider(Protocol):
    """Minimum contract implemented by every adapter owned by the application."""

    def info(self) -> ProviderInfo: ...

    async def aclose(self) -> None: ...
