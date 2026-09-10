"""OpenAI-compatible embedding adapter with bounded retry and strict response checks."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx

from enterprise_rag.adapters.embeddings.common import EmbeddingError, batches, validated_vectors
from enterprise_rag.domain.errors import ErrorCode
from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind

Sleeper = Callable[[float], Awaitable[None]]


class OpenAICompatibleEmbedding:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimension: int,
        batch_size: int = 64,
        max_batch_tokens: int = 8_192,
        timeout_seconds: float = 20.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        if not base_url.strip() or not api_key.strip() or not model.strip():
            raise ValueError("remote embedding configuration must not be blank")
        if dimension <= 0 or batch_size <= 0 or max_batch_tokens <= 0:
            raise ValueError("embedding dimension and batch limits must be positive")
        if timeout_seconds <= 0 or not 0 <= max_retries <= 10:
            raise ValueError("timeout and retry settings are invalid")
        self._endpoint = base_url.rstrip("/") + "/embeddings"
        self._api_key = api_key
        self._model = model
        self._dimension = dimension
        self._batch_size = batch_size
        self._max_batch_tokens = max_batch_tokens
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._sleeper = sleeper
        self._closed = False

    @property
    def dimension(self) -> int:
        return self._dimension

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            kind=ProviderKind.EMBEDDING,
            name="openai_compatible",
            version=self._model,
            capabilities=frozenset({"documents", "query", "normalized", "retry", "batch"}),
            is_remote=True,
            health=ProviderHealth.UNAVAILABLE if self._closed else ProviderHealth.UNKNOWN,
        )

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if self._closed:
            raise RuntimeError("Embedding Provider is closed")
        result: list[list[float]] = []
        for batch in batches(texts, max_items=self._batch_size, max_tokens=self._max_batch_tokens):
            result.extend(await self._embed_batch(batch))
        return result

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents((text,)))[0]

    async def aclose(self) -> None:
        if self._closed:
            return
        if self._owns_client:
            await self._client.aclose()
        self._closed = True

    async def _embed_batch(self, texts: tuple[str, ...]) -> list[list[float]]:
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(
                    self._endpoint,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"model": self._model, "input": list(texts), "encoding_format": "float"},
                    timeout=self._timeout,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if attempt >= self._max_retries:
                    raise EmbeddingError(
                        ErrorCode.EMBEDDING_UNAVAILABLE,
                        "The remote embedding Provider is unavailable.",
                    ) from error
                await self._sleeper(0.25 * (2**attempt))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self._max_retries:
                    await self._sleeper(0.25 * (2**attempt))
                    continue
                raise EmbeddingError(
                    ErrorCode.EMBEDDING_UNAVAILABLE,
                    "The remote embedding Provider exhausted bounded retries.",
                    {"status_code": response.status_code},
                )
            if response.status_code >= 400:
                raise EmbeddingError(
                    ErrorCode.EMBEDDING_UNAVAILABLE,
                    "The remote embedding Provider rejected the request.",
                    {"status_code": response.status_code},
                )
            return self._parse_response(response, len(texts))
        raise AssertionError("retry loop did not terminate")

    def _parse_response(self, response: httpx.Response, expected_count: int) -> list[list[float]]:
        try:
            payload: Any = response.json()
            entries = payload["data"]
            if not isinstance(entries, list):
                raise TypeError("data is not a list")
            indexed: dict[int, Sequence[float]] = {}
            for entry in entries:
                index = entry["index"]
                vector = entry["embedding"]
                if not isinstance(index, int) or index in indexed or not isinstance(vector, list):
                    raise TypeError("invalid embedding entry")
                indexed[index] = vector
            if set(indexed) != set(range(expected_count)):
                raise ValueError("embedding indexes do not match input")
            ordered = [indexed[index] for index in range(expected_count)]
        except (KeyError, TypeError, ValueError) as error:
            raise EmbeddingError(
                ErrorCode.EMBEDDING_INVALID_RESPONSE,
                "The remote embedding Provider returned an invalid response.",
            ) from error
        return validated_vectors(ordered, expected_count=expected_count, dimension=self.dimension)
