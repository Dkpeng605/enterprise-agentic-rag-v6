import math
import os
from collections.abc import Iterable, Sequence
from pathlib import Path

import httpx
import pytest

from enterprise_rag.adapters.rerankers import (
    LocalFastEmbedReranker,
    NoopReranker,
    OpenAICompatibleReranker,
)
from enterprise_rag.domain import AppError, ErrorCode
from enterprise_rag.ports import RerankCandidate


def candidates() -> tuple[RerankCandidate, ...]:
    return (
        RerankCandidate(f"leaf_{'a' * 64}", "artificial intelligence", 0.3),
        RerankCandidate(f"leaf_{'b' * 64}", "banana bread", 0.2),
        RerankCandidate(f"leaf_{'c' * 64}", "machine learning", 0.1),
    )


class FakeCrossEncoder:
    def __init__(self, scores: Sequence[float]) -> None:
        self.scores = scores
        self.calls: list[tuple[str, tuple[str, ...], int]] = []

    def rerank(
        self, query: str, documents: Iterable[str], batch_size: int = 64
    ) -> Iterable[float]:
        self.calls.append((query, tuple(documents), batch_size))
        return iter(self.scores)


@pytest.mark.anyio
async def test_local_cross_encoder_maps_scores_and_uses_stable_ties() -> None:
    model = FakeCrossEncoder((0.5, 0.2, 0.5))
    provider = LocalFastEmbedReranker(model=model, batch_size=2)

    result = await provider.rerank("AI", candidates(), top_k=2)

    assert [entry.candidate_id for entry in result] == [
        candidates()[0].candidate_id,
        candidates()[2].candidate_id,
    ]
    assert model.calls == [
        ("AI", ("artificial intelligence", "banana bread", "machine learning"), 2)
    ]
    assert provider.info().name == "local_cross_encoder"


@pytest.mark.anyio
@pytest.mark.parametrize("scores", [(0.1,), (0.1, math.nan, 0.2)])
async def test_local_cross_encoder_rejects_bad_model_output(scores: Sequence[float]) -> None:
    provider = LocalFastEmbedReranker(model=FakeCrossEncoder(scores))

    with pytest.raises(AppError) as raised:
        await provider.rerank("AI", candidates(), top_k=2)

    assert raised.value.code is ErrorCode.RERANKER_INVALID_RESPONSE


@pytest.mark.anyio
async def test_http_provider_aligns_indexes_and_retries_transient_status() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url == "https://provider.example/v1/rerank"
        assert request.headers["Authorization"] == "Bearer test-secret"
        if calls == 1:
            return httpx.Response(429, json={"error": "retry"})
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 2, "relevance_score": 0.8},
                    {"index": 0, "relevance_score": 0.9},
                ]
            },
        )

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleReranker(
            base_url="https://provider.example/v1",
            api_key="test-secret",
            model="rerank-model",
            client=client,
            sleeper=sleeper,
        )
        result = await provider.rerank("AI", candidates(), top_k=2)

    assert calls == 2
    assert sleeps == [0.25]
    assert [entry.candidate_id for entry in result] == [
        candidates()[2].candidate_id,
        candidates()[0].candidate_id,
    ]


@pytest.mark.anyio
async def test_http_provider_bounds_timeout_retries() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("upstream detail", request=request)

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleReranker(
            base_url="https://provider.example/v1",
            api_key="not-in-errors",
            model="rerank-model",
            client=client,
            sleeper=sleeper,
        )
        with pytest.raises(AppError) as raised:
            await provider.rerank("AI", candidates(), top_k=2)

    assert calls == 3
    assert sleeps == [0.25, 0.5]
    assert raised.value.code is ErrorCode.RERANKER_UNAVAILABLE
    assert "upstream detail" not in str(raised.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "payload", "expected_code", "expected_calls"),
    [
        (401, {"error": "secret diagnostic"}, ErrorCode.RERANKER_UNAVAILABLE, 1),
        (
            200,
            {"results": [{"index": 9, "relevance_score": 1.0}]},
            ErrorCode.RERANKER_INVALID_RESPONSE,
            1,
        ),
        (
            200,
            {
                "results": [
                    {"index": 0, "relevance_score": 1.0},
                    {"index": 0, "relevance_score": 0.5},
                ]
            },
            ErrorCode.RERANKER_INVALID_RESPONSE,
            1,
        ),
    ],
)
async def test_http_provider_rejects_bad_response_without_leaking_body(
    status: int, payload: object, expected_code: ErrorCode, expected_calls: int
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleReranker(
            base_url="https://provider.example/v1",
            api_key="not-in-errors",
            model="rerank-model",
            client=client,
        )
        with pytest.raises(AppError) as raised:
            await provider.rerank("AI", candidates(), top_k=2)

    assert calls == expected_calls
    assert raised.value.code is expected_code
    assert "secret diagnostic" not in str(raised.value)
    assert "not-in-errors" not in str(raised.value)


@pytest.mark.anyio
async def test_noop_is_explicit_non_degraded_fused_selection_and_close_is_idempotent() -> None:
    provider = NoopReranker()
    result = await provider.rerank("AI", candidates(), top_k=2)
    assert [(entry.candidate_id, entry.score) for entry in result] == [
        (candidates()[0].candidate_id, 0.3),
        (candidates()[1].candidate_id, 0.2),
    ]

    await provider.aclose()
    await provider.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await provider.rerank("AI", candidates(), top_k=2)


@pytest.mark.model
@pytest.mark.skipif(os.getenv("RUN_MODEL_TESTS") != "1", reason="real model test is opt-in")
@pytest.mark.anyio
async def test_real_local_cross_encoder_ranks_english_relevance(tmp_path: Path) -> None:
    provider = LocalFastEmbedReranker(cache_dir=tmp_path / "models")
    result = await provider.rerank("What is artificial intelligence?", candidates(), top_k=2)
    assert len(result) == 2
    assert result[0].candidate_id == candidates()[0].candidate_id
    assert all(math.isfinite(entry.score) for entry in result)
