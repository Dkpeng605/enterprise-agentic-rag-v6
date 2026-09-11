"""HTTP reranker adapter using a common OpenAI-adjacent rerank schema."""

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx

from enterprise_rag.adapters.rerankers.common import RerankerError, validate_request
from enterprise_rag.domain.errors import ErrorCode
from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind
from enterprise_rag.ports.reranker import RerankCandidate, RerankResult

Sleeper = Callable[[float], Awaitable[None]]


class OpenAICompatibleReranker:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 20.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        if not base_url.strip() or not api_key.strip() or not model.strip():
            raise ValueError("remote reranker configuration must not be blank")
        if timeout_seconds <= 0 or not 0 <= max_retries <= 10:
            raise ValueError("timeout and retry settings are invalid")
        self._endpoint = base_url.rstrip("/") + "/rerank"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._sleeper = sleeper
        self._closed = False

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            kind=ProviderKind.RERANKER,
            name="openai_compatible",
            version=self._model,
            capabilities=frozenset({"cross-encoder", "http", "retry"}),
            is_remote=True,
            health=ProviderHealth.UNAVAILABLE if self._closed else ProviderHealth.UNKNOWN,
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
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(
                    self._endpoint,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": self._model,
                        "query": query,
                        "documents": [candidate.text for candidate in candidates],
                        "top_n": top_k,
                    },
                    timeout=self._timeout,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if attempt >= self._max_retries:
                    raise RerankerError(
                        ErrorCode.RERANKER_UNAVAILABLE,
                        "The remote reranker Provider is unavailable.",
                    ) from error
                await self._sleeper(0.25 * (2**attempt))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self._max_retries:
                    await self._sleeper(0.25 * (2**attempt))
                    continue
                raise RerankerError(
                    ErrorCode.RERANKER_UNAVAILABLE,
                    "The remote reranker Provider exhausted bounded retries.",
                    {"status_code": response.status_code},
                )
            if response.status_code >= 400:
                raise RerankerError(
                    ErrorCode.RERANKER_UNAVAILABLE,
                    "The remote reranker Provider rejected the request.",
                    {"status_code": response.status_code},
                )
            return self._parse_response(response, candidates, top_k)
        raise AssertionError("retry loop did not terminate")

    async def aclose(self) -> None:
        if self._closed:
            return
        if self._owns_client:
            await self._client.aclose()
        self._closed = True

    @staticmethod
    def _parse_response(
        response: httpx.Response,
        candidates: Sequence[RerankCandidate],
        expected_count: int,
    ) -> list[RerankResult]:
        try:
            payload: Any = response.json()
            entries = payload["results"]
            if not isinstance(entries, list) or len(entries) != expected_count:
                raise TypeError("results count is invalid")
            indexed: dict[int, float] = {}
            for entry in entries:
                index = entry["index"]
                score = entry["relevance_score"]
                if (
                    isinstance(index, bool)
                    or not isinstance(index, int)
                    or not 0 <= index < len(candidates)
                    or index in indexed
                    or isinstance(score, bool)
                    or not isinstance(score, (int, float))
                    or not math.isfinite(float(score))
                ):
                    raise TypeError("rerank result is invalid")
                indexed[index] = float(score)
            return [
                RerankResult(candidates[index].candidate_id, score)
                for index, score in indexed.items()
            ]
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise RerankerError(
                ErrorCode.RERANKER_INVALID_RESPONSE,
                "The remote reranker Provider returned an invalid response.",
            ) from error
