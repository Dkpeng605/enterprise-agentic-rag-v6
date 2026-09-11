"""Lazy local CrossEncoder reranker backed by FastEmbed ONNX."""

import asyncio
import math
from collections.abc import Iterable, Sequence
from importlib.metadata import version
from pathlib import Path
from typing import Protocol, cast

from fastembed.rerank.cross_encoder import TextCrossEncoder

from enterprise_rag.adapters.rerankers.common import RerankerError, validate_request
from enterprise_rag.domain.errors import ErrorCode
from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind
from enterprise_rag.ports.reranker import RerankCandidate, RerankResult

DEFAULT_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"


class _CrossEncoderModel(Protocol):
    def rerank(
        self, query: str, documents: Iterable[str], batch_size: int = 64
    ) -> Iterable[float]: ...


class LocalFastEmbedReranker:
    def __init__(
        self,
        *,
        model_name: str = DEFAULT_MODEL,
        cache_dir: Path | None = None,
        batch_size: int = 32,
        model: _CrossEncoderModel | None = None,
    ) -> None:
        if not model_name.strip() or batch_size <= 0:
            raise ValueError("reranker model and batch size must be valid")
        self._model_name = model_name
        self._cache_dir = cache_dir
        self._batch_size = batch_size
        self._model = model
        self._closed = False

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            kind=ProviderKind.RERANKER,
            name="local_cross_encoder",
            version=f"{self._model_name}@fastembed-{version('fastembed')}",
            capabilities=frozenset({"cross-encoder", "onnx", "local"}),
            is_remote=False,
            health=ProviderHealth.UNAVAILABLE if self._closed else ProviderHealth.HEALTHY,
        )

    async def rerank(
        self,
        query: str,
        candidates: Sequence[RerankCandidate],
        *,
        top_k: int,
    ) -> list[RerankResult]:
        if self._closed:
            raise RuntimeError("Reranker Provider is closed")
        validate_request(query, candidates, top_k)
        try:
            scores = await asyncio.to_thread(self._rerank_sync, query, candidates)
        except RerankerError:
            raise
        except Exception as error:
            raise RerankerError(
                ErrorCode.RERANKER_UNAVAILABLE,
                "The local reranker Provider is unavailable.",
            ) from error
        if len(scores) != len(candidates):
            raise RerankerError(
                ErrorCode.RERANKER_INVALID_RESPONSE,
                "The local reranker Provider returned an invalid response.",
            )
        ranked: list[tuple[int, RerankResult]] = []
        for index, (candidate, score) in enumerate(zip(candidates, scores, strict=True)):
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise RerankerError(
                    ErrorCode.RERANKER_INVALID_RESPONSE,
                    "The local reranker Provider returned an invalid response.",
                )
            value = float(score)
            if not math.isfinite(value):
                raise RerankerError(
                    ErrorCode.RERANKER_INVALID_RESPONSE,
                    "The local reranker Provider returned an invalid response.",
                )
            ranked.append((index, RerankResult(candidate.candidate_id, value)))
        ranked.sort(key=lambda item: (-item[1].score, item[0]))
        return [item[1] for item in ranked[:top_k]]

    async def aclose(self) -> None:
        self._model = None
        self._closed = True

    def _rerank_sync(
        self, query: str, candidates: Sequence[RerankCandidate]
    ) -> list[float]:
        if self._model is None:
            self._model = cast(
                _CrossEncoderModel,
                TextCrossEncoder(
                    model_name=self._model_name,
                    cache_dir=str(self._cache_dir) if self._cache_dir is not None else None,
                    lazy_load=True,
                ),
            )
        return list(
            self._model.rerank(
                query,
                (candidate.text for candidate in candidates),
                batch_size=self._batch_size,
            )
        )
