import math
import os
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from enterprise_rag.adapters.embeddings import (
    LocalMultilingualEmbedding,
    OpenAICompatibleEmbedding,
)
from enterprise_rag.domain import AppError, ErrorCode


class FakeLocalModel:
    def __init__(self, responses: Sequence[Sequence[Sequence[float]]]) -> None:
        self.responses = list(responses)
        self.batches: list[tuple[str, ...]] = []

    def embed(self, documents: Sequence[str], *, batch_size: int) -> Iterable[Any]:
        self.batches.append(tuple(documents))
        assert batch_size == len(documents)
        return iter(self.responses.pop(0))


@pytest.mark.anyio
async def test_local_provider_batches_normalizes_and_preserves_order() -> None:
    model = FakeLocalModel(
        (
            ((3.0, 4.0, 0.0), (0.0, 0.0, 2.0)),
            ((1.0, 1.0, 1.0),),
        )
    )
    provider = LocalMultilingualEmbedding(
        model=model, dimension=3, batch_size=2, max_batch_tokens=10
    )

    vectors = await provider.embed_documents(("中文", "English", "third"))

    assert model.batches == [("中文", "English"), ("third",)]
    assert vectors[0] == pytest.approx([0.6, 0.8, 0.0])
    assert vectors[1] == pytest.approx([0.0, 0.0, 1.0])
    assert all(
        math.sqrt(sum(value * value for value in vector)) == pytest.approx(1.0)
        for vector in vectors
    )
    assert provider.dimension == 3
    assert "mean-pooling" in provider.info().version


@pytest.mark.anyio
async def test_local_provider_rejects_input_and_invalid_model_outputs() -> None:
    for response in (((1.0, 2.0),), ((float("nan"), 1.0, 2.0),), ((0.0, 0.0, 0.0),)):
        provider = LocalMultilingualEmbedding(
            model=FakeLocalModel((response,)), dimension=3, max_batch_tokens=5
        )
        with pytest.raises(AppError) as raised:
            await provider.embed_documents(("valid",))
        assert raised.value.code is ErrorCode.EMBEDDING_INVALID_RESPONSE

    provider = LocalMultilingualEmbedding(model=FakeLocalModel(()), dimension=3, max_batch_tokens=2)
    for texts in (("",), ("one two three",)):
        with pytest.raises(AppError) as raised:
            await provider.embed_documents(texts)
        assert raised.value.code is ErrorCode.EMBEDDING_INPUT_INVALID


@pytest.mark.anyio
async def test_openai_compatible_orders_response_and_retries_only_transient_errors() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["Authorization"] == "Bearer test-secret"
        if calls == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 2.0]},
                    {"index": 0, "embedding": [3.0, 4.0]},
                ]
            },
        )

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleEmbedding(
            base_url="https://provider.example/v1",
            api_key="test-secret",
            model="embed-model",
            dimension=2,
            client=client,
            sleeper=sleeper,
        )
        vectors = await provider.embed_documents(("first", "second"))

    assert calls == 2
    assert sleeps == [0.25]
    assert vectors[0] == pytest.approx([0.6, 0.8])
    assert vectors[1] == pytest.approx([0.0, 1.0])


@pytest.mark.anyio
async def test_remote_auth_and_malformed_responses_do_not_leak_provider_body() -> None:
    for status, payload, expected_calls in (
        (401, {"error": "secret diagnostic"}, 1),
        (200, {"data": [{"index": 4, "embedding": [1.0, 0.0]}]}, 1),
    ):
        calls = 0

        def handler(
            request: httpx.Request,
            response_status: int = status,
            response_payload: object = payload,
        ) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(response_status, json=response_payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = OpenAICompatibleEmbedding(
                base_url="https://provider.example/v1",
                api_key="not-in-errors",
                model="embed-model",
                dimension=2,
                client=client,
            )
            with pytest.raises(AppError) as raised:
                await provider.embed_query("query")
        assert calls == expected_calls
        assert "secret diagnostic" not in str(raised.value)
        assert "not-in-errors" not in str(raised.value)
        assert raised.value.code in {
            ErrorCode.EMBEDDING_UNAVAILABLE,
            ErrorCode.EMBEDDING_INVALID_RESPONSE,
        }


@pytest.mark.anyio
async def test_provider_close_is_idempotent() -> None:
    provider = LocalMultilingualEmbedding(model=FakeLocalModel((((1.0, 0.0),),)), dimension=2)
    await provider.aclose()
    await provider.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await provider.embed_query("query")


@pytest.mark.model
@pytest.mark.skipif(os.getenv("RUN_MODEL_TESTS") != "1", reason="real model test is opt-in")
@pytest.mark.anyio
async def test_real_multilingual_minilm_embeds_chinese_and_english(tmp_path: Path) -> None:
    provider = LocalMultilingualEmbedding(cache_dir=tmp_path / "models")
    vectors = await provider.embed_documents(("企业知识库", "enterprise knowledge base"))
    assert len(vectors) == 2
    assert all(len(vector) == 384 for vector in vectors)
    assert all(math.isfinite(value) for vector in vectors for value in vector)
