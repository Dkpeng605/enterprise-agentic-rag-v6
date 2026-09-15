"""Framework-neutral sparse projection contract."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from enterprise_rag.domain.common import require_non_empty
from enterprise_rag.ports.provider import Provider


class SparseMode(StrEnum):
    """How sparse terms are projected and queried by the VectorStore."""

    PRECOMPUTED = "precomputed"
    MILVUS_BUILTIN_BM25 = "milvus_builtin_bm25"


@dataclass(frozen=True, slots=True)
class SparseEncoding:
    """One honest sparse input: either a vector or text consumed by BM25."""

    mode: SparseMode
    vector: Mapping[int, float] | None = None
    text: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", SparseMode(self.mode))
        if self.mode is SparseMode.PRECOMPUTED:
            if not self.vector or self.text is not None:
                raise ValueError("precomputed sparse encoding requires only a vector")
            object.__setattr__(self, "vector", MappingProxyType(dict(self.vector)))
            return
        if self.vector is not None or self.text is None:
            raise ValueError("Milvus BM25 sparse encoding requires only text")
        require_non_empty(self.text, "text")

    def require_vector(self) -> Mapping[int, float]:
        if self.vector is None:
            raise ValueError("this sparse encoding is text-backed")
        return self.vector


class SparseEncoder(Provider, Protocol):
    @property
    def mode(self) -> SparseMode: ...

    async def encode_documents(self, texts: Sequence[str]) -> list[SparseEncoding]: ...

    async def encode_query(self, text: str) -> SparseEncoding: ...
