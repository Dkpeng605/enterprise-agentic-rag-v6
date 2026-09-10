"""Framework-neutral dense embedding contract."""

from collections.abc import Sequence
from typing import Protocol

from enterprise_rag.ports.provider import Provider


class EmbeddingProvider(Provider, Protocol):
    @property
    def dimension(self) -> int: ...

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...
