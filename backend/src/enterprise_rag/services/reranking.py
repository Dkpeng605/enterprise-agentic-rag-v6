"""Candidate reranking with strict identity alignment and safe RRF fallback."""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.domain.retrieval import RetrievalHit
from enterprise_rag.ports.reranker import RerankCandidate, Reranker


@dataclass(frozen=True, slots=True)
class RerankItem:
    hit: RetrievalHit
    retrieval_text: str

    def __post_init__(self) -> None:
        if not self.retrieval_text.strip():
            raise ValueError("retrieval_text must not be blank")


@dataclass(frozen=True, slots=True)
class RerankOutcome:
    hits: tuple[RetrievalHit, ...]
    provider: str
    candidate_count: int
    degraded: bool
    error_code: ErrorCode | None = None


class RerankingService:
    def __init__(
        self,
        provider: Reranker,
        *,
        rerank_candidates: int = 20,
        selected_leaf_k: int = 8,
    ) -> None:
        if rerank_candidates <= 0 or not 0 < selected_leaf_k <= rerank_candidates:
            raise ValueError("rerank candidate limits are invalid")
        self._provider = provider
        self._rerank_candidates = rerank_candidates
        self._selected_leaf_k = selected_leaf_k

    async def rerank(self, query: str, items: Sequence[RerankItem]) -> RerankOutcome:
        if not query.strip():
            raise ValueError("query must not be blank")
        self._validate_items(items)
        candidates = tuple(items[: self._rerank_candidates])
        provider_name = self._provider.info().name
        if not candidates:
            return RerankOutcome((), provider_name, 0, False)
        selected_count = min(self._selected_leaf_k, len(candidates))
        request = tuple(
            RerankCandidate(item.hit.leaf_id, item.retrieval_text, item.hit.fused_score)
            for item in candidates
        )
        try:
            results = await self._provider.rerank(query, request, top_k=selected_count)
            scores = self._validate_results(
                results,
                candidate_ids={item.hit.leaf_id for item in candidates},
                expected_count=selected_count,
            )
        except Exception as error:
            code = (
                error.code
                if isinstance(error, AppError)
                and error.code
                in {ErrorCode.RERANKER_UNAVAILABLE, ErrorCode.RERANKER_INVALID_RESPONSE}
                else ErrorCode.RERANKER_INVALID_RESPONSE
            )
            return RerankOutcome(
                tuple(self._selected_hit(item.hit, None) for item in candidates[:selected_count]),
                provider_name,
                len(candidates),
                True,
                code,
            )
        original_order = {item.hit.leaf_id: index for index, item in enumerate(candidates)}
        selected_items = [item for item in candidates if item.hit.leaf_id in scores]
        selected_items.sort(
            key=lambda item: (-scores[item.hit.leaf_id], original_order[item.hit.leaf_id])
        )
        return RerankOutcome(
            tuple(
                self._selected_hit(item.hit, scores[item.hit.leaf_id]) for item in selected_items
            ),
            provider_name,
            len(candidates),
            False,
        )

    @staticmethod
    def _validate_items(items: Sequence[RerankItem]) -> None:
        ids = [item.hit.leaf_id for item in items]
        if len(ids) != len(set(ids)):
            raise ValueError("rerank items must have unique leaf IDs")

    @staticmethod
    def _validate_results(
        results: object,
        *,
        candidate_ids: set[str],
        expected_count: int,
    ) -> dict[str, float]:
        if not isinstance(results, list) or len(results) != expected_count:
            raise ValueError("reranker result count is invalid")
        scores: dict[str, float] = {}
        for result in results:
            candidate_id = getattr(result, "candidate_id", None)
            score = getattr(result, "score", None)
            if (
                not isinstance(candidate_id, str)
                or candidate_id not in candidate_ids
                or candidate_id in scores
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(float(score))
            ):
                raise ValueError("reranker result identity is invalid")
            scores[candidate_id] = float(score)
        return scores

    @staticmethod
    def _selected_hit(hit: RetrievalHit, score: float | None) -> RetrievalHit:
        return RetrievalHit(
            leaf_id=hit.leaf_id,
            root_id=hit.root_id,
            dense_rank=hit.dense_rank,
            sparse_rank=hit.sparse_rank,
            fused_score=hit.fused_score,
            rerank_score=score,
            selected=True,
        )
