import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

import pytest

from enterprise_rag.domain import AppError, ErrorCode, RetrievalHit
from enterprise_rag.ports import (
    ProviderHealth,
    ProviderInfo,
    ProviderKind,
    RerankCandidate,
    RerankResult,
)
from enterprise_rag.services import RerankingService, RerankItem


def hit(letter: str, fused_score: float) -> RetrievalHit:
    return RetrievalHit(
        leaf_id=f"leaf_{letter * 64}",
        root_id=f"root_{letter * 64}",
        dense_rank=1,
        sparse_rank=None,
        fused_score=fused_score,
        rerank_score=None,
        selected=False,
    )


def item(letter: str, fused_score: float) -> RerankItem:
    return RerankItem(hit(letter, fused_score), f"text {letter}")


class FakeReranker:
    def __init__(self, results: object) -> None:
        self.results = results
        self.calls: list[tuple[str, tuple[RerankCandidate, ...], int]] = []

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            ProviderKind.RERANKER,
            "fake",
            "1",
            frozenset({"test"}),
            False,
            ProviderHealth.HEALTHY,
        )

    async def rerank(
        self,
        query: str,
        candidates: Sequence[RerankCandidate],
        *,
        top_k: int,
    ) -> list[RerankResult]:
        self.calls.append((query, tuple(candidates), top_k))
        if isinstance(self.results, Exception):
            raise self.results
        return cast(list[RerankResult], self.results)

    async def aclose(self) -> None:
        return None


@pytest.mark.anyio
async def test_maps_ids_orders_scores_and_bounds_candidate_funnel() -> None:
    items = tuple(item(letter, 1 / index) for index, letter in enumerate("abcdef", start=1))
    provider = FakeReranker(
        [
            RerankResult(items[2].hit.leaf_id, 0.7),
            RerankResult(items[0].hit.leaf_id, 0.9),
        ]
    )
    service = RerankingService(provider, rerank_candidates=4, selected_leaf_k=2)

    outcome = await service.rerank("question", items)

    assert [result.leaf_id for result in outcome.hits] == [
        items[0].hit.leaf_id,
        items[2].hit.leaf_id,
    ]
    assert [result.rerank_score for result in outcome.hits] == [0.9, 0.7]
    assert all(result.selected for result in outcome.hits)
    assert outcome.candidate_count == 4
    assert outcome.degraded is False
    assert provider.calls[0][2] == 2
    assert [value.candidate_id for value in provider.calls[0][1]] == [
        entry.hit.leaf_id for entry in items[:4]
    ]


@pytest.mark.anyio
async def test_equal_scores_preserve_original_rrf_order() -> None:
    items = (item("a", 0.3), item("b", 0.2))
    provider = FakeReranker(
        [RerankResult(items[1].hit.leaf_id, 0.5), RerankResult(items[0].hit.leaf_id, 0.5)]
    )

    outcome = await RerankingService(provider, selected_leaf_k=2).rerank("q", items)

    assert [result.leaf_id for result in outcome.hits] == [
        items[0].hit.leaf_id,
        items[1].hit.leaf_id,
    ]


@dataclass
class MalformedResult:
    candidate_id: str
    score: object


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["missing", "duplicate", "unknown", "nan"])
async def test_bad_provider_response_degrades_to_stable_rrf_selection(kind: str) -> None:
    items = (item("a", 0.3), item("b", 0.2), item("c", 0.1))
    valid = RerankResult(items[0].hit.leaf_id, 0.9)
    responses: dict[str, object] = {
        "missing": [valid],
        "duplicate": [valid, valid],
        "unknown": [valid, RerankResult(f"leaf_{'z' * 64}", 0.8)],
        "nan": [valid, MalformedResult(items[1].hit.leaf_id, math.nan)],
    }

    outcome = await RerankingService(
        FakeReranker(responses[kind]), selected_leaf_k=2
    ).rerank("q", items)

    assert [result.leaf_id for result in outcome.hits] == [
        items[0].hit.leaf_id,
        items[1].hit.leaf_id,
    ]
    assert all(result.rerank_score is None for result in outcome.hits)
    assert outcome.degraded is True
    assert outcome.error_code is ErrorCode.RERANKER_INVALID_RESPONSE


@pytest.mark.anyio
async def test_provider_failure_is_sanitized_and_empty_input_skips_provider() -> None:
    provider = FakeReranker(
        AppError(ErrorCode.RERANKER_UNAVAILABLE, "secret upstream diagnostic")
    )
    service = RerankingService(provider)

    outcome = await service.rerank("q", (item("a", 0.3),))
    empty = await service.rerank("q", ())

    assert outcome.degraded is True
    assert outcome.error_code is ErrorCode.RERANKER_UNAVAILABLE
    assert "secret" not in repr(outcome)
    assert empty.hits == ()
    assert len(provider.calls) == 1


@pytest.mark.anyio
async def test_duplicate_input_identity_is_rejected_before_provider_call() -> None:
    duplicate = item("a", 0.3)
    provider = FakeReranker([])

    with pytest.raises(ValueError, match="unique"):
        await RerankingService(provider).rerank("q", (duplicate, duplicate))

    assert provider.calls == []
