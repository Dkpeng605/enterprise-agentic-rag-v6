"""Framework-neutral sparse text encoding contract."""

from collections.abc import Mapping, Sequence
from typing import Protocol

from enterprise_rag.ports.provider import Provider


class SparseEncoder(Provider, Protocol):
    async def encode_documents(self, texts: Sequence[str]) -> list[Mapping[int, float]]: ...

    async def encode_query(self, text: str) -> Mapping[int, float]: ...
