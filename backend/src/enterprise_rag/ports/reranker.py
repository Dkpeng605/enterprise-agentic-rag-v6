"""Framework-neutral candidate reranking contract."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from enterprise_rag.domain.common import require_non_empty
from enterprise_rag.ports.provider import Provider


@dataclass(frozen=True, slots=True)
class RerankCandidate:
    candidate_id: str
    text: str
    fused_score: float

    def __post_init__(self) -> None:
        require_non_empty(self.candidate_id, "candidate_id")
        require_non_empty(self.text, "text")
        if not math.isfinite(self.fused_score):
            raise ValueError("fused_score must be finite")


@dataclass(frozen=True, slots=True)
class RerankResult:
    candidate_id: str
    score: float

    def __post_init__(self) -> None:
        require_non_empty(self.candidate_id, "candidate_id")
        if not math.isfinite(self.score):
            raise ValueError("rerank score must be finite")


class Reranker(Provider, Protocol):
    async def rerank(
        self,
        query: str,
        candidates: Sequence[RerankCandidate],
        *,
        top_k: int,
    ) -> list[RerankResult]: ...
