"""Stable multilingual hashing sparse encoder for lexical projection."""

import hashlib
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence

from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind

_LEXICAL_TOKEN = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_]+", re.UNICODE)
_MAX_INDEX = 2**31 - 1


class HashingSparseEncoder:
    def __init__(self) -> None:
        self._closed = False

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            kind=ProviderKind.SPARSE_ENCODER,
            name="hashing_lexical",
            version="blake2b-31bit-logtf-l2-v1",
            capabilities=frozenset({"documents", "query", "multilingual", "deterministic"}),
            is_remote=False,
            health=ProviderHealth.UNAVAILABLE if self._closed else ProviderHealth.HEALTHY,
        )

    async def encode_documents(self, texts: Sequence[str]) -> list[Mapping[int, float]]:
        self._ensure_open()
        return [self._encode(text) for text in texts]

    async def encode_query(self, text: str) -> Mapping[int, float]:
        self._ensure_open()
        return self._encode(text)

    async def aclose(self) -> None:
        self._closed = True

    @staticmethod
    def _encode(text: str) -> Mapping[int, float]:
        tokens = [match.group().casefold() for match in _LEXICAL_TOKEN.finditer(text)]
        if not tokens:
            raise AppError(
                ErrorCode.EMBEDDING_INPUT_INVALID,
                "Sparse encoding input must contain lexical tokens.",
            )
        counts: Counter[int] = Counter()
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            counts[int.from_bytes(digest, "big") % _MAX_INDEX] += 1
        weighted = {index: 1.0 + math.log(count) for index, count in counts.items()}
        norm = math.sqrt(sum(value * value for value in weighted.values()))
        return {index: value / norm for index, value in sorted(weighted.items())}

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Sparse Encoder is closed")
