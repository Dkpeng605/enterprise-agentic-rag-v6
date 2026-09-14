"""Lightweight ONNX multilingual embedding adapter backed by FastEmbed."""

import asyncio
import re
from collections.abc import Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any, Protocol, cast

from fastembed import TextEmbedding

from enterprise_rag.adapters.embeddings.common import (
    batches,
    estimate_token_count,
    validated_vectors,
)
from enterprise_rag.domain.errors import AppError, ErrorCode
from enterprise_rag.ports.provider import ProviderHealth, ProviderInfo, ProviderKind

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
BGE_SMALL_ZH_MODEL = "BAAI/bge-small-zh-v1.5"


@dataclass(frozen=True, slots=True)
class FastEmbedModelProfile:
    """Non-secret metadata for a selectable local FastEmbed model."""

    model_name: str
    dimension: int
    registry_input_token_limit: int | None
    language_note: str


FASTEMBED_MODEL_PROFILES = {
    DEFAULT_MODEL: FastEmbedModelProfile(
        DEFAULT_MODEL,
        384,
        512,
        "multilingual",
    ),
    BGE_SMALL_ZH_MODEL: FastEmbedModelProfile(
        BGE_SMALL_ZH_MODEL,
        512,
        512,
        "Chinese-focused",
    ),
}


class _FastEmbedModel(Protocol):
    def embed(self, documents: Sequence[str], *, batch_size: int) -> Iterable[Any]: ...

    def token_count(self, texts: str) -> int: ...


_INPUT_LIMIT = re.compile(r"(?P<limit>\d+)\s+input tokens", re.IGNORECASE)


class LocalMultilingualEmbedding:
    def __init__(
        self,
        *,
        model_name: str = DEFAULT_MODEL,
        cache_dir: Path | None = None,
        batch_size: int = 32,
        max_batch_tokens: int = 8_192,
        model: _FastEmbedModel | None = None,
        dimension: int | None = None,
    ) -> None:
        if batch_size <= 0 or max_batch_tokens <= 0:
            raise ValueError("embedding batch limits must be positive")
        self._model_name = model_name
        self._cache_dir = cache_dir
        self._batch_size = batch_size
        self._max_batch_tokens = max_batch_tokens
        self._model = model
        self._input_token_limit = self._model_input_token_limit(model_name)
        self._runtime_limit_probed = False
        self._untruncated_tokenizer: Any | None = None
        self._dimension = dimension or int(TextEmbedding.get_embedding_size(model_name))
        if self._dimension <= 0:
            raise ValueError("embedding dimension must be positive")
        profile = FASTEMBED_MODEL_PROFILES.get(model_name)
        if (
            model is None
            and profile is not None
            and dimension is not None
            and dimension != profile.dimension
        ):
            raise ValueError(
                f"embedding dimension {dimension} does not match {model_name} ({profile.dimension})"
            )
        self._closed = False

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def input_token_limit(self) -> int | None:
        """The model context limit reported by FastEmbed's model registry."""

        return self._input_token_limit

    @property
    def tokenizer_name(self) -> str:
        return f"fastembed-tokenizer:{self._model_name}"

    @property
    def model_name(self) -> str:
        return self._model_name

    def info(self) -> ProviderInfo:
        return ProviderInfo(
            kind=ProviderKind.EMBEDDING,
            name="local_multilingual_minilm",
            version=f"{self._model_name}@fastembed-{version('fastembed')}:mean-pooling",
            capabilities=frozenset(
                {
                    "documents",
                    "query",
                    "normalized",
                    "multilingual",
                    "onnx",
                    "model_tokenizer",
                    "input_token_limit",
                    "model_profile",
                }
            ),
            is_remote=False,
            health=ProviderHealth.UNAVAILABLE if self._closed else ProviderHealth.HEALTHY,
        )

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if self._closed:
            raise RuntimeError("Embedding Provider is closed")
        result: list[list[float]] = []
        for batch in batches(
            texts,
            max_items=self._batch_size,
            max_tokens=self._max_batch_tokens,
            token_counter=self.count_tokens,
            max_input_tokens=self._input_token_limit,
        ):
            try:
                raw = await asyncio.to_thread(self._embed_sync, batch)
            except AppError:
                raise
            except Exception as error:
                raise AppError(
                    ErrorCode.EMBEDDING_UNAVAILABLE,
                    "The local embedding model could not produce vectors.",
                ) from error
            result.extend(
                validated_vectors(raw, expected_count=len(batch), dimension=self.dimension)
            )
        return result

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents((text,)))[0]

    async def aclose(self) -> None:
        self._model = None
        self._untruncated_tokenizer = None
        self._closed = True

    def count_tokens(self, text: str) -> int:
        """Count with the exact FastEmbed tokenizer whenever it is available."""

        model = self._ensure_model()
        self._sync_input_token_limit(model)
        if (count := self._count_with_untruncated_tokenizer(model, text)) is not None:
            return count
        counter = getattr(model, "token_count", None)
        if callable(counter):
            return max(1, int(counter(text)))
        return estimate_token_count(text)

    def warm_tokenizer(self) -> None:
        """Load the tokenizer metadata before a splitter is composed around this provider."""

        self._sync_input_token_limit(self._ensure_model())

    @staticmethod
    def _model_input_token_limit(model_name: str) -> int | None:
        description = TextEmbedding._get_model_description(model_name).description
        match = _INPUT_LIMIT.search(description)
        return int(match.group("limit")) if match else None

    def _embed_sync(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._ensure_model()
        vectors = model.embed(texts, batch_size=len(texts))
        return [list(map(float, vector)) for vector in vectors]

    def _ensure_model(self) -> _FastEmbedModel:
        if self._model is None:
            self._model = cast(
                _FastEmbedModel,
                TextEmbedding(
                    model_name=self._model_name,
                    cache_dir=str(self._cache_dir) if self._cache_dir is not None else None,
                    lazy_load=True,
                ),
            )
        return self._model

    def _count_with_untruncated_tokenizer(
        self, model: _FastEmbedModel, text: str
    ) -> int | None:
        """Count the complete input while the runtime model remains truncated safely.

        FastEmbed's public ``token_count`` follows the tokenizer's truncation setting.
        Some cached model packages therefore return the input limit for every longer
        string. The splitter needs the complete count to make progress, while the
        embedding call must keep the original truncated tokenizer. A private copy gives
        us both behaviours without mutating the model used for inference.
        """

        if self._untruncated_tokenizer is None:
            runtime_model = getattr(model, "model", None)
            tokenizer = getattr(runtime_model, "tokenizer", None)
            if tokenizer is None or not callable(getattr(tokenizer, "encode", None)):
                return None
            try:
                candidate = deepcopy(tokenizer)
                disable_truncation = getattr(candidate, "no_truncation", None)
                if not callable(disable_truncation):
                    return None
                disable_truncation()
            except Exception:
                return None
            self._untruncated_tokenizer = candidate
        try:
            encoding = self._untruncated_tokenizer.encode(text)
            ids = getattr(encoding, "ids", None)
            if not isinstance(ids, list | tuple):
                return None
            return max(1, len(ids))
        except Exception:
            return None

    def _sync_input_token_limit(self, model: _FastEmbedModel) -> None:
        inner = getattr(model, "model", None)
        tokenizer = getattr(inner, "tokenizer", None)
        truncation = getattr(tokenizer, "truncation", None)
        limit = truncation.get("max_length") if isinstance(truncation, dict) else None
        if isinstance(limit, int) and limit > 1:
            self._input_token_limit = limit
            self._runtime_limit_probed = True
            return

        # FastEmbed wrappers without a visible truncation field need one bounded
        # probe. Repeating the probe from every Splitter count_tokens call turns
        # a small document into thousands of unnecessary tokenizer passes.
        if self._runtime_limit_probed:
            return

        # Some FastEmbed ONNX wrappers expose only token_count(). Their model
        # registry description can be stale (for example, it may say 512 while
        # the bundled tokenizer actually truncates at 128), so probe beyond the
        # advertised limit and use a smaller capped result when one is observed.
        counter = getattr(model, "token_count", None)
        advertised = self._input_token_limit
        if not callable(counter) or advertised is None:
            self._runtime_limit_probed = True
            return
        try:
            observed = int(counter("x " * max(advertised * 2, 1_024)))
            if 1 < observed < advertised:
                self._input_token_limit = observed
        finally:
            self._runtime_limit_probed = True
