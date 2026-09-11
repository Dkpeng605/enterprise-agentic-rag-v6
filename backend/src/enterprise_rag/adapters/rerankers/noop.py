"""Explicit no-op reranker preserving fused order."""

from collections.abc import Sequence

from enterprise_rag.adapters.rerankers.common import validate_request
from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind
from enterprise_rag.ports.reranker import RerankCandidate, RerankResult


class NoopReranker:
    def __init__(self) -> None:
        self._closed = False

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            kind=ProviderKind.RERANKER,
            name="none",
            version="fused-order-v1",
            capabilities=frozenset({"noop", "deterministic"}),
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
        return [
            RerankResult(item.candidate_id, item.fused_score) for item in candidates[:top_k]
        ]

    async def aclose(self) -> None:
        self._closed = True
