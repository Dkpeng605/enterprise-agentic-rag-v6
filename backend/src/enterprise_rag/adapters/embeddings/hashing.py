"""Small deterministic dense embedding used by the offline E2E runtime."""

import hashlib
import math
import re
from collections.abc import Sequence

from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind

_TOKEN = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_]+", re.UNICODE)


class HashingDenseEmbedding:
    """Dependency-free lexical projection, intentionally not a semantic model."""

    def __init__(self, *, dimension: int = 128) -> None:
        if dimension <= 1:
            raise ValueError("embedding dimension must be greater than one")
        self._dimension = dimension
        self._closed = False

    @property
    def dimension(self) -> int:
        return self._dimension

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            kind=ProviderKind.EMBEDDING,
            name="hashing_dense_e2e",
            version=f"blake2b-signed-l2-v1:{self._dimension}",
            capabilities=frozenset(
                {"documents", "query", "normalized", "deterministic", "offline", "e2e"}
            ),
            is_remote=False,
            health=ProviderHealth.UNAVAILABLE if self._closed else ProviderHealth.HEALTHY,
        )

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self._ensure_open()
        return [self._embed(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        self._ensure_open()
        return self._embed(text)

    async def aclose(self) -> None:
        self._closed = True

    def _embed(self, text: str) -> list[float]:
        tokens = [match.group().casefold() for match in _TOKEN.finditer(text)]
        if not tokens:
            raise AppError(
                ErrorCode.EMBEDDING_INPUT_INVALID,
                "Dense hashing input must contain lexical tokens.",
            )
        vector = [0.0] * self._dimension
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
            index = int.from_bytes(digest[:8], "big") % self._dimension
            sign = 1.0 if digest[8] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector]

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Embedding Provider is closed")
