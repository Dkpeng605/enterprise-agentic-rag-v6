"""Deterministic reciprocal-rank fusion across methods and sub-queries."""

from dataclasses import dataclass

from enterprise_rag.domain.retrieval import RetrievalHit
from enterprise_rag.services.retrieval import DualSearchResult, SearchMethod


@dataclass(frozen=True, slots=True)
class FusionDiagnostic:
    ranked_list_count: int
    input_hit_count: int
    unique_leaf_count: int
    root_quota_dropped: int
    top_k_dropped: int


@dataclass(frozen=True, slots=True)
class FusionResult:
    hits: tuple[RetrievalHit, ...]
    diagnostic: FusionDiagnostic


@dataclass(slots=True)
class _Accumulator:
    root_id: str
    score: float = 0.0
    dense_rank: int | None = None
    sparse_rank: int | None = None


class ReciprocalRankFusion:
    def __init__(self, *, rrf_k: int = 60, top_k: int = 30, max_leaves_per_root: int = 3) -> None:
        if rrf_k <= 0:
            raise ValueError("rrf_k must be positive")
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        if max_leaves_per_root <= 0:
            raise ValueError("max_leaves_per_root must be positive")
        self._rrf_k = rrf_k
        self._top_k = top_k
        self._max_leaves_per_root = max_leaves_per_root

    def fuse(self, results: tuple[DualSearchResult, ...]) -> FusionResult:
        accumulators: dict[str, _Accumulator] = {}
        input_count = 0
        ranked_list_count = 0
        for result in results:
            for branch in (result.dense, result.sparse):
                ranked_list_count += 1
                seen_in_list: set[str] = set()
                for rank, vector_hit in enumerate(branch.hits, start=1):
                    input_count += 1
                    if vector_hit.leaf_id in seen_in_list:
                        raise ValueError("a ranked list contains a duplicate leaf ID")
                    seen_in_list.add(vector_hit.leaf_id)
                    current = accumulators.get(vector_hit.leaf_id)
                    if current is None:
                        current = _Accumulator(vector_hit.root_id)
                        accumulators[vector_hit.leaf_id] = current
                    elif current.root_id != vector_hit.root_id:
                        raise ValueError("one leaf ID maps to conflicting root IDs")
                    current.score += 1.0 / (self._rrf_k + rank)
                    if branch.method is SearchMethod.DENSE:
                        current.dense_rank = self._minimum(current.dense_rank, rank)
                    else:
                        current.sparse_rank = self._minimum(current.sparse_rank, rank)

        ordered = sorted(
            accumulators.items(),
            key=lambda item: (-item[1].score, item[0]),
        )
        selected: list[RetrievalHit] = []
        root_counts: dict[str, int] = {}
        root_quota_dropped = 0
        top_k_dropped = 0
        for leaf_id, item in ordered:
            if root_counts.get(item.root_id, 0) >= self._max_leaves_per_root:
                root_quota_dropped += 1
                continue
            if len(selected) >= self._top_k:
                top_k_dropped += 1
                continue
            selected.append(
                RetrievalHit(
                    leaf_id=leaf_id,
                    root_id=item.root_id,
                    dense_rank=item.dense_rank,
                    sparse_rank=item.sparse_rank,
                    fused_score=item.score,
                    rerank_score=None,
                    selected=False,
                )
            )
            root_counts[item.root_id] = root_counts.get(item.root_id, 0) + 1
        return FusionResult(
            tuple(selected),
            FusionDiagnostic(
                ranked_list_count,
                input_count,
                len(accumulators),
                root_quota_dropped,
                top_k_dropped,
            ),
        )

    @staticmethod
    def _minimum(current: int | None, candidate: int) -> int:
        return candidate if current is None else min(current, candidate)
